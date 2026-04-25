"""Thin async client for an OpenAI-compatible chat-completions endpoint.

Default backend is the local llama.cpp server on port 8080. When
``LLM_BACKEND=openrouter`` is set in the environment, the same calls are
routed to OpenRouter instead — used by parallel-agent worktrees that don't
have access to the Pi's llama.cpp server. Both backends speak the same
OpenAI SSE/JSON dialect, so the parsing helpers don't branch.

Entry points:
  * `stream_chat(messages)` — async generator yielding token deltas for live
    Telegram message edits.
  * `chat(messages)` — non-streaming one-shot, used by the push scheduler where
    we wait for the full reply before sending.
  * `health()` — status string for /status.

SSE parsing and non-stream response parsing are factored into pure helpers so
they can be unit-tested without standing up an HTTP mock.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

import httpx


LLAMA_BASE = "http://127.0.0.1:8080"
LLAMA_URL = f"{LLAMA_BASE}/v1/chat/completions"
HEALTH_URL = f"{LLAMA_BASE}/health"
MODEL = "gemma4"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "google/gemma-4-26b-a4b-it:free"

_DONE = "[DONE]"


@dataclass(frozen=True)
class Backend:
    """Where to send chat-completion requests."""

    name: str
    url: str
    model: str
    headers: dict[str, str]


def _get_backend() -> Backend:
    """Pick a backend from env each call so tests and live config can swap freely."""
    if os.getenv("LLM_BACKEND", "").strip().lower() == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "LLM_BACKEND=openrouter but OPENROUTER_API_KEY is empty"
            )
        return Backend(
            name="openrouter",
            url=OPENROUTER_URL,
            model=os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_OPENROUTER_MODEL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    return Backend(
        name="llama.cpp",
        url=LLAMA_URL,
        model=MODEL,
        headers={},
    )


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
    """Stream token deltas from the configured backend as they arrive."""
    backend = _get_backend()
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            backend.url,
            headers=backend.headers,
            json={
                "model": backend.model,
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
    backend = _get_backend()
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            backend.url,
            headers=backend.headers,
            json={
                "model": backend.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
        )
        r.raise_for_status()
        return _parse_completion(r.json())


async def health() -> str:
    """Return the backend's self-reported status, or an error string.

    OpenRouter has no public health endpoint, so we report the configured
    model name without making a request — keeps `/status` snappy.
    """
    backend = _get_backend()
    if backend.name == "openrouter":
        return f"openrouter (model={backend.model})"
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
