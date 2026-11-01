"""Shared helpers for the model-backed evals: run the real pipeline, call a judge model."""

import asyncio
import json
import re
import time

import httpx

from app.config import get_settings
from app.llm import LLMError, stream_chat
from app.prompt import build_messages
from app.retrieval import Index, search

JUDGE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


async def generate(
    index: Index, http: httpx.AsyncClient, question: str, model: str, history=()
) -> dict:
    """Answer one question through the app's own retrieval, prompt and LLM client."""
    settings = get_settings().model_copy(update={"openrouter_models": model})
    result = search(index, question)
    messages, sources = build_messages(index, list(history), question, result)
    started, text, used, finish, error = time.monotonic(), "", None, None, None
    try:
        async for kind, value in stream_chat(messages, settings, http):
            if kind == "token":
                text += value
            elif kind == "model":
                used = value
            elif kind == "finish":
                finish = value
    except LLMError as e:
        error = f"{e.status}: {e.detail[:160]}"
    return {
        "question": question,
        "answer": text,
        "model": used,
        "finish": finish,
        "error": error,
        "latency_s": round(time.monotonic() - started, 2),
        "context": messages[-1]["content"],
        "sources": sources,
    }


async def complete(http: httpx.AsyncClient, model: str, prompt: str, tries: int = 4) -> str:
    """One non-streaming completion, retrying 429s and 5xx with backoff."""
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
        "HTTP-Referer": settings.app_url,
        "X-Title": f"{settings.app_title} evals",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 4000,
        "reasoning": {"effort": "low", "exclude": True},
    }
    for attempt in range(tries):
        r = await http.post(settings.openrouter_url, json=payload, headers=headers, timeout=120)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        status = r.status_code if r.status_code != 200 else (body.get("error") or {}).get("code")
        if not status and body.get("choices"):
            return body["choices"][0]["message"].get("content") or ""
        if attempt + 1 < tries and status in (429, 500, 502, 503, None):
            await asyncio.sleep(10 * (attempt + 1))
            continue
        raise RuntimeError(f"judge call failed: {status} {str(body)[:200]}")
    raise RuntimeError("unreachable")


def parse_json(text: str) -> dict:
    """The first JSON object in a model reply (models sometimes wrap it in prose or fences)."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"no JSON object in: {text[:200]!r}")
    return json.loads(match.group(0))


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]
