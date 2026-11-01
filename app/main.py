"""FastAPI app: /api/chat streams a grounded answer as server-sent events."""

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.constraints import follow_up, parse
from app.llm import LLMError, stream_chat
from app.prompt import build_messages
from app.retrieval import embed_query, load_index, search

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("app")
MAX_QUESTION_CHARS = 2000


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.index = load_index(settings.index_dir)
    embed_query("warm up")  # load the embedding model now, not on the first question
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_s, connect=10))
    yield
    await app.state.http.aclose()


app = FastAPI(
    title="Dar Global & Wasalt Assistant", docs_url=None, redoc_url=None, lifespan=lifespan
)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def ends_with_question(self):
        last = self.messages[-1]
        if last.role != "user" or not last.content.strip():
            raise ValueError("the last message must be a non-empty user message")
        if len(last.content) > MAX_QUESTION_CHARS:
            raise ValueError(f"questions are limited to {MAX_QUESTION_CHARS} characters")
        return self


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/healthz")
def healthz() -> dict:
    manifest = app.state.index.manifest
    return {
        "status": "ok",
        "index": manifest["docs_sha256"][:12],
        "chunks": manifest["chunks"],
        "models": get_settings().models,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    settings, index = get_settings(), app.state.index
    question = req.messages[-1].content.strip()
    history = [m.model_dump() for m in req.messages[:-1]]
    previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    filters = follow_up(parse(question), parse(previous))
    query = f"{previous}\n{question}" if previous else question

    async def events():
        started, status, model, n_tokens, finish = time.monotonic(), "ok", None, 0, None
        result = None
        try:
            result = await run_in_threadpool(search, index, query, None, filters)
            messages, sources = build_messages(index, history, question, result)
            yield sse("sources", {"sources": sources})
            async for kind, value in stream_chat(messages, settings, app.state.http):
                if kind == "model":
                    model = value
                elif kind == "finish":
                    finish = value
                else:
                    n_tokens += 1
                    yield sse("token", {"text": value})
            if finish == "length":
                status = "truncated"
                yield sse(
                    "token", {"text": "\n\n(The answer was cut off. Ask a narrower question.)"}
                )
            yield sse("done", {"model": model})
        except LLMError as e:
            status = f"llm_error_{e.status}"
            log.warning(json.dumps({"event": "llm_error", "status": e.status, "detail": e.detail}))
            yield sse("error", {"message": e.user_message})
        except Exception:
            status = "error"
            log.exception("chat failed")
            yield sse("error", {"message": "Something went wrong. Please try again."})
        finally:
            # No message text and no client IP in logs.
            log.info(
                json.dumps(
                    {
                        "event": "chat",
                        "status": status,
                        "model": model,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "turns": len(req.messages),
                        "question_chars": len(question),
                        "sources": len(result.chunks) if result else 0,
                        "filters": filters.describe() if filters else None,
                        "relaxed": result.relaxed if result else None,
                        "stream_events": n_tokens,
                        "finish": finish,
                    }
                )
            )

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )
