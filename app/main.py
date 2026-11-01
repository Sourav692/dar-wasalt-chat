"""FastAPI app: /api/chat streams a grounded answer as server-sent events."""

import hmac
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import ROOT, get_settings
from app.constraints import follow_up, parse
from app.llm import LLMError, stream_chat
from app.prompt import build_messages
from app.retrieval import embed_query, load_index, search
from app.security import (
    FORBIDDEN_CHARS,
    BodyLimit,
    IPHasher,
    RateLimiter,
    SecurityHeaders,
    client_ip,
    client_key,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("app")
MAX_QUESTION_CHARS = 2000
MAX_MESSAGES = 13  # 6 earlier turns + the new question
# 13 messages x 4,000 chars x up to 2 bytes (Arabic UTF-8) is ~104 KB, plus JSON overhead.
MAX_BODY_BYTES = 128 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.index = load_index(settings.index_dir)
    embed_query("warm up")  # load the embedding model now, not on the first question
    salt = settings.log_salt.get_secret_value() if settings.log_salt else None
    app.state.hash_ip = IPHasher(salt)
    app.state.limiter = RateLimiter(settings.rate_per_minute, settings.rate_per_day)
    app.state.global_limiter = RateLimiter(settings.global_per_minute, settings.global_per_day)
    app.state.auth_fail_limiter = RateLimiter(
        settings.auth_fail_per_minute, settings.auth_fail_per_day
    )
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_s, connect=10))
    yield
    await app.state.http.aclose()


app = FastAPI(
    title="Dar Global & Wasalt Assistant",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(BodyLimit, max_bytes=MAX_BODY_BYTES)
app.add_middleware(SecurityHeaders)
STATIC = ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)  # answers can run past 2,000 chars

    @field_validator("content")
    @classmethod
    def no_control_chars(cls, value: str) -> str:
        if FORBIDDEN_CHARS.search(value):
            raise ValueError("message contains control or bidi-override characters")
        return value


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)

    @model_validator(mode="after")
    def ends_with_question(self):
        last = self.messages[-1]
        if last.role != "user" or not last.content.strip():
            raise ValueError("the last message must be a non-empty user message")
        if len(last.content) > MAX_QUESTION_CHARS:
            raise ValueError(f"questions are limited to {MAX_QUESTION_CHARS} characters")
        return self


def too_many(wait: float, message: str) -> HTTPException:
    return HTTPException(
        status_code=429, detail=message, headers={"Retry-After": str(math.ceil(wait))}
    )


async def rate_limit(request: Request) -> None:
    """Per-client limit. Runs before the passcode check, so guesses are throttled too.

    async on purpose: it runs on the event loop, so RateLimiter.check never races itself.
    """
    key = client_key(client_ip(request, get_settings().trusted_proxy_hops))
    ip_hash = request.state.ip_hash = app.state.hash_ip(key)
    if (wait := app.state.limiter.check(ip_hash)) is not None:
        log.info(json.dumps({"event": "rate_limited", "scope": "client", "ip_hash": ip_hash}))
        raise too_many(wait, "Too many messages. Please wait a moment and try again.")


async def require_passcode(x_access_code: str | None = Header(default=None)) -> None:
    """When ACCESS_PASSCODE is set, /api/chat needs a matching X-Access-Code header.

    A correct code always gets through, so nobody can lock real users out. Wrong codes from
    all clients share one budget: past it they get 429 instead of 401 and a warning is
    logged. The real defence against guessing is the per-client limit plus a long random code.
    """
    expected = get_settings().access_passcode
    if expected is None:
        return
    given = (x_access_code or "").encode()
    if hmac.compare_digest(given, expected.get_secret_value().encode()):
        return
    if (wait := app.state.auth_fail_limiter.check("*")) is not None:
        log.warning(json.dumps({"event": "passcode_failures_over_budget"}))
        raise too_many(wait, "Too many wrong access codes. Please try again later.")
    raise HTTPException(status_code=401, detail="Access code required.")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/healthz")
def healthz() -> dict:
    manifest = app.state.index.manifest
    return {
        "status": "ok",
        "index": manifest["docs_sha256"][:12],
        "chunks": manifest["chunks"],
        "snapshot_dates": manifest.get("snapshot_dates", []),
        "models": get_settings().models,
    }


# Rate limit first, so passcode guesses are limited too.
@app.post("/api/chat", dependencies=[Depends(rate_limit), Depends(require_passcode)])
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    # Service-wide quota is charged only now, after the passcode and body validation passed,
    # so rejected requests cannot use it up.
    if (wait := app.state.global_limiter.check("*")) is not None:
        log.info(json.dumps({"event": "rate_limited", "scope": "global"}))
        raise too_many(wait, "The assistant is busy. Please try again later.")
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
                        "ip_hash": request.state.ip_hash,
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
