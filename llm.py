"""Thin async client for llama.cpp's OpenAI-compatible HTTP server.

Exposes three entry points:
  * `stream_chat(messages)` — async generator yielding token deltas for live
    Telegram message edits.
  * `chat(messages)` — non-streaming one-shot, used by the push scheduler where
    we wait for the full reply before sending.
  * `health()` — status string for /model.

SSE parsing and non-stream response parsing are factored into pure helpers so
they can be unit-tested without standing up an HTTP mock.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Awaitable, Callable

import httpx


LLAMA_BASE = "http://127.0.0.1:8080"
LLAMA_URL = f"{LLAMA_BASE}/v1/chat/completions"
HEALTH_URL = f"{LLAMA_BASE}/health"
MODEL = "gemma4"

_DONE = "[DONE]"


def _parse_sse_delta(raw: str) -> str | None:
    """Return the content delta from one SSE line, or None to skip/terminate."""
    if not raw.startswith("data:"):
        return None
    payload = raw[5:].strip()
    if not payload or payload == _DONE:
        return None
    try:
        chunk = json.loads(payload)
        return chunk["choices"][0]["delta"].get("content") or None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


def _parse_completion(payload: dict) -> str:
    return payload["choices"][0]["message"].get("content", "")


async def stream_chat(
    messages: list[dict],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """Stream token deltas from llama.cpp as they arrive."""
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            LLAMA_URL,
            json={
                "model": MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                delta = _parse_sse_delta(raw)
                if delta:
                    yield delta


async def chat(
    messages: list[dict],
    *,
    max_tokens: int = 256,
    temperature: float = 0.8,
) -> str:
    """Return the full assistant reply as a single string (no streaming)."""
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            LLAMA_URL,
            json={
                "model": MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
        )
        r.raise_for_status()
        return _parse_completion(r.json())


async def health() -> str:
    """Return the llama.cpp server's self-reported status, or an error string."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(HEALTH_URL)
            return r.json().get("status", "unknown")
    except Exception as e:  # noqa: BLE001 — surface the reason verbatim to the user
        return f"unreachable ({e})"


_BENCH_PROMPT = [{"role": "user", "content": "Reply with one short sentence."}]


async def bench(
    *,
    chat_fn: Callable[..., Awaitable[str]] | None = None,
    timeout: float = 30.0,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Run a tiny prompt through the model and return a one-line perf summary.

    On timeout returns ``"model not responding"``; on other errors returns a
    short ``"error: <ExceptionType>"`` string. Injected collaborators keep this
    unit-testable without an HTTP server.
    """
    if chat_fn is None:
        chat_fn = chat
    t0 = now()
    try:
        text = await asyncio.wait_for(
            chat_fn(_BENCH_PROMPT, max_tokens=32, temperature=0.2),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "model not responding"
    except Exception as e:  # noqa: BLE001 — surface the class name to the user
        return f"error: {type(e).__name__}"
    elapsed = now() - t0
    chars = len(text)
    tok_per_s = (chars / 4) / elapsed if elapsed > 0 else 0.0
    return f"{chars} chars in {elapsed:.1f}s (~{tok_per_s:.1f} tok/s)"
