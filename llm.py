"""Thin async client for an OpenAI-compatible chat-completions endpoint.

Default backend is a local OpenAI-compatible server on port 8080. When
``LLM_BACKEND=openrouter`` is set in the environment, the same calls are
routed to OpenRouter instead (the production backend). Both speak the same
OpenAI SSE/JSON dialect, so the parsing helpers don't branch.

Entry points:
  * `chat(messages)` — one-shot completion used by just-talk replies, the
    daily story composer, and the drill judge.
  * `health()` — status string for /status.
  * `usage()` — single-line backend usage / quota summary for /status.

Response parsing is factored into a pure helper so it can be unit-tested
without standing up an HTTP mock.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


LLAMA_BASE = "http://127.0.0.1:8080"
LLAMA_URL = f"{LLAMA_BASE}/v1/chat/completions"
HEALTH_URL = f"{LLAMA_BASE}/health"
MODEL = "gemma4"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_AUTH_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
DEFAULT_OPENROUTER_MODEL = "google/gemma-4-26b-a4b-it:free"

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


def _parse_completion(payload: dict) -> str:
    # Some models (e.g. reasoning-only on the openrouter/free auto-router)
    # return content: null on a 200; treat that the same as empty so callers
    # always get a string.
    return payload["choices"][0]["message"].get("content") or ""


async def chat(
    messages: list[dict],
    *,
    max_tokens: int = 256,
    temperature: float = 0.8,
    disable_reasoning: bool = False,
    timeout: float = 180,
) -> str:
    """Return the full assistant reply as a single string (no streaming).

    When ``disable_reasoning=True`` the request body carries
    ``reasoning: {enabled: false}`` — an OpenRouter-normalised hint that suppresses
    the model's chain-of-thought trace. Needed for callers that share a small
    ``max_tokens`` budget with reasoning-heavy free-tier auto-router targets,
    which would otherwise spend the whole budget on ``message.reasoning`` and
    return ``content: null``. Default ``False`` keeps every existing caller's
    on-the-wire payload byte-identical.
    """
    backend = _get_backend()
    body: dict = {
        "model": backend.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if disable_reasoning:
        body["reasoning"] = {"enabled": False}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(backend.url, headers=backend.headers, json=body)
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


async def usage() -> str:
    """One-line backend usage / quota summary for /status.

    For the local llama backend there is no notion of quota — returns
    ``"n/a"`` without making any network call. For OpenRouter, GETs
    ``/api/v1/auth/key`` with the configured Bearer header and renders
    ``$<usage> used / limit: <limit-or-unlimited>, rate <r> req / <interval>``.

    Transport errors and non-2xx responses are caught and returned as
    ``"unavailable (<reason>)"`` so `/status` keeps rendering. A misconfigured
    backend (``LLM_BACKEND=openrouter`` with no key) raises from
    ``_get_backend`` — parity with `health()`.
    """
    backend = _get_backend()
    if backend.name != "openrouter":
        return "n/a"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(OPENROUTER_AUTH_KEY_URL, headers=backend.headers)
            r.raise_for_status()
            data = r.json().get("data") or {}
    except Exception as e:  # noqa: BLE001 — surface short reason to /status
        return f"unavailable ({type(e).__name__})"
    spent = data.get("usage")
    limit = data.get("limit")
    rate = data.get("rate_limit") or {}
    spent_str = f"${spent:.3f}" if isinstance(spent, (int, float)) else "$?"
    limit_str = (
        f"${limit:.2f}" if isinstance(limit, (int, float)) else "unlimited"
    )
    req = rate.get("requests")
    interval = rate.get("interval")
    if req is not None and interval:
        rate_str = f", rate {req} req / {interval}"
    else:
        rate_str = ""
    return f"{spent_str} used / limit: {limit_str}{rate_str}"
