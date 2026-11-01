"""Streaming client for OpenRouter chat completions with a model fallback list."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.config import Settings


class LLMError(Exception):
    """A failure the user should see as a short message; details go to the logs only."""

    def __init__(self, status: int | None, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail

    @property
    def user_message(self) -> str:
        if self.status == 429:
            return "The free model is busy right now. Please try again in a minute."
        return "The assistant is unavailable right now. Please try again shortly."


async def stream_chat(
    messages: list[dict], settings: Settings, client: httpx.AsyncClient
) -> AsyncIterator[tuple[str, str]]:
    """Yield ("model", id) once, ("token", text) pieces, then ("finish", reason).

    Raises LLMError on failure, including a stream that produced no answer text.
    """
    payload = {
        "models": settings.models,  # OpenRouter tries these in order
        "messages": messages,
        "stream": True,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "reasoning": {"effort": "low", "exclude": True},
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
        "HTTP-Referer": settings.app_url,
        "X-Title": settings.app_title,
    }
    model_sent, produced, finish = False, False, None
    try:
        async with (
            asyncio.timeout(settings.llm_deadline_s),
            client.stream("POST", settings.openrouter_url, json=payload, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:500]
                raise LLMError(resp.status_code, body)
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                data = line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if err := chunk.get("error"):  # mid-stream failure inside a 200 response
                    raise LLMError(err.get("code"), str(err.get("message"))[:500])
                if not model_sent and chunk.get("model"):
                    model_sent = True
                    yield "model", chunk["model"]
                for choice in chunk.get("choices", []):
                    finish = choice.get("finish_reason") or finish
                    if text := (choice.get("delta") or {}).get("content"):
                        produced = True
                        yield "token", text
    except (TimeoutError, httpx.TimeoutException) as e:
        raise LLMError(None, f"timeout: {e!r}") from e
    except httpx.HTTPError as e:
        raise LLMError(None, f"network: {e!r}") from e
    except json.JSONDecodeError as e:
        raise LLMError(None, f"bad stream chunk: {e}") from e
    if not produced:
        raise LLMError(None, f"stream ended without content (finish_reason={finish})")
    yield "finish", finish or "unknown"
