"""Tests for llm.py parse helpers and backend dispatch (no network)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import llm


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Wipe the LLM env vars so each test starts from defaults."""
    for key in ("LLM_BACKEND", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def captured_request(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch httpx.AsyncClient to record one POST and reply with a canned body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def test_parse_completion_returns_content() -> None:
    payload = {"choices": [{"message": {"content": "paragraph here"}}]}
    assert llm._parse_completion(payload) == "paragraph here"


def test_parse_completion_missing_content_returns_empty() -> None:
    payload = {"choices": [{"message": {}}]}
    assert llm._parse_completion(payload) == ""


def test_parse_completion_null_content_returns_empty() -> None:
    # OpenRouter's auto-router can land on reasoning-only models that emit
    # `content: null` on a 200; callers must still get a string back so a
    # downstream `len(text)` doesn't blow up.
    payload = {"choices": [{"message": {"content": None}}]}
    assert llm._parse_completion(payload) == ""


# _get_backend ----------------------------------------------------------


def test_backend_defaults_to_llama(clean_env: pytest.MonkeyPatch) -> None:
    backend = llm._get_backend()
    assert backend.name == "llama.cpp"
    assert backend.url == llm.LLAMA_URL
    assert backend.model == llm.MODEL
    assert backend.headers == {}


def test_backend_unknown_value_falls_back_to_llama(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_BACKEND", "claude")
    assert llm._get_backend().name == "llama.cpp"


def test_backend_openrouter_with_key(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    backend = llm._get_backend()
    assert backend.name == "openrouter"
    assert backend.url == llm.OPENROUTER_URL
    assert backend.model == llm.DEFAULT_OPENROUTER_MODEL
    assert backend.headers == {"Authorization": "Bearer sk-test"}


def test_backend_openrouter_uses_custom_model(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    clean_env.setenv("OPENROUTER_MODEL", "anthropic/claude-3-haiku")
    assert llm._get_backend().model == "anthropic/claude-3-haiku"


def test_backend_openrouter_is_case_insensitive(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_BACKEND", "OpenRouter")
    clean_env.setenv("OPENROUTER_API_KEY", "k")
    assert llm._get_backend().name == "openrouter"


def test_backend_openrouter_without_key_raises(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_BACKEND", "openrouter")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm._get_backend()


# chat() request shape --------------------------------------------------


def test_chat_default_posts_to_llama_without_auth(
    clean_env: pytest.MonkeyPatch,
    captured_request: dict,
) -> None:
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert captured_request["method"] == "POST"
    assert captured_request["url"] == llm.LLAMA_URL
    assert "authorization" not in captured_request["headers"]
    assert captured_request["body"]["model"] == llm.MODEL
    assert captured_request["body"]["stream"] is False


def test_chat_openrouter_posts_with_bearer_and_model(
    clean_env: pytest.MonkeyPatch,
    captured_request: dict,
) -> None:
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-secret")
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert captured_request["url"] == llm.OPENROUTER_URL
    assert captured_request["headers"]["authorization"] == "Bearer sk-secret"
    assert captured_request["body"]["model"] == llm.DEFAULT_OPENROUTER_MODEL


# chat() disable_reasoning kwarg ---------------------------------------


def test_chat_default_body_omits_reasoning_key(
    clean_env: pytest.MonkeyPatch,
    captured_request: dict,
) -> None:  # AC1 — default kwargs => no "reasoning" key
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert "reasoning" not in captured_request["body"], (
        "AC1 — default chat() must NOT carry a 'reasoning' key on the wire; "
        f"got body={captured_request['body']!r}"
    )


def test_chat_disable_reasoning_sets_reasoning_enabled_false(
    clean_env: pytest.MonkeyPatch,
    captured_request: dict,
) -> None:  # AC2 — disable_reasoning=True adds reasoning: {enabled: False}
    asyncio.run(
        llm.chat([{"role": "user", "content": "hi"}], disable_reasoning=True)
    )
    assert captured_request["body"].get("reasoning") == {"enabled": False}, (
        "AC2 — disable_reasoning=True must place reasoning={'enabled': False} on "
        f"the wire; got {captured_request['body'].get('reasoning')!r}"
    )


def test_chat_disable_reasoning_preserves_default_body_shape(
    clean_env: pytest.MonkeyPatch,
    captured_request: dict,
) -> None:  # AC2 — body still carries model/messages/max_tokens/temperature/stream
    msgs = [{"role": "user", "content": "hi"}]
    asyncio.run(llm.chat(msgs, disable_reasoning=True))
    body = captured_request["body"]
    assert body["model"] == llm.MODEL, f"model lost from body; got {body!r}"
    assert body["messages"] == msgs, f"messages lost from body; got {body!r}"
    assert body["max_tokens"] == 256, f"default max_tokens lost; got {body!r}"
    assert body["temperature"] == 0.8, f"default temperature lost; got {body!r}"
    assert body["stream"] is False, f"stream flag lost; got {body!r}"


def test_chat_default_body_byte_identical_to_explicit_false(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # edge: disable_reasoning=False default is byte-identical to current
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content) if request.content else {})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    msgs = [{"role": "user", "content": "hi"}]
    asyncio.run(llm.chat(msgs))
    asyncio.run(llm.chat(msgs, disable_reasoning=False))
    assert bodies[0] == bodies[1], (
        "edge — default kwargs and disable_reasoning=False must produce "
        f"byte-identical bodies; default={bodies[0]!r}, explicit={bodies[1]!r}"
    )


def test_chat_disable_reasoning_works_on_llama_backend(
    clean_env: pytest.MonkeyPatch,
    captured_request: dict,
) -> None:  # edge: llama.cpp backend accepts the unknown reasoning field
    # Default backend is llama.cpp (clean_env wipes LLM_BACKEND).
    asyncio.run(
        llm.chat([{"role": "user", "content": "hi"}], disable_reasoning=True)
    )
    assert captured_request["url"] == llm.LLAMA_URL, (
        f"clean_env should keep llama.cpp default; got {captured_request['url']!r}"
    )
    assert captured_request["body"].get("reasoning") == {"enabled": False}, (
        "edge — reasoning field must be present even on the llama.cpp backend "
        "(server is expected to ignore unknown fields); got "
        f"{captured_request['body'].get('reasoning')!r}"
    )


# chat() timeout kwarg ---------------------------------------------------


def _capture_client_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch httpx.AsyncClient to record its constructor kwargs and reply OK."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        captured.update(kwargs)
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def test_chat_timeout_kwarg_reaches_async_client(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # chat(..., timeout=10) → httpx.AsyncClient(timeout=10)
    captured = _capture_client_kwargs(monkeypatch)
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], timeout=10))
    assert captured.get("timeout") == 10, (
        f"chat(timeout=10) must construct AsyncClient with timeout=10; "
        f"got {captured.get('timeout')!r}"
    )


def test_chat_default_timeout_is_180(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # default keeps the long-story budget: AsyncClient(timeout=180)
    captured = _capture_client_kwargs(monkeypatch)
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))
    assert captured.get("timeout") == 180, (
        f"chat() default must construct AsyncClient with timeout=180; "
        f"got {captured.get('timeout')!r}"
    )


# health() --------------------------------------------------------------


def test_health_openrouter_returns_synthetic_string(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "k")
    out = asyncio.run(llm.health())
    assert out.startswith("openrouter (model=")
    assert llm.DEFAULT_OPENROUTER_MODEL in out


def test_health_llama_unreachable_returns_error_string(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = asyncio.run(llm.health())
    assert "unreachable" in out


# usage() ---------------------------------------------------------------


def _patch_usage_response(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response | Exception,
) -> dict:
    """Capture one outbound request and reply with `response` (or raise it)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        if isinstance(response, Exception):
            raise response
        return response

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def _block_all_http(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace httpx.AsyncClient with a transport that fails on any call.

    Used to prove usage() makes no network call when not on openrouter.
    """
    calls: dict = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise AssertionError(
            f"usage() must not call out to {request.url!r} on this backend"
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


def test_usage_returns_na_for_default_backend(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 — default (llama) backend → "n/a"
    _block_all_http(monkeypatch)
    out = asyncio.run(llm.usage())
    assert out == "n/a", (
        f"AC4 — default backend usage() must return literal 'n/a'; got {out!r}"
    )


def test_usage_returns_na_for_unknown_backend(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 — anything other than openrouter → "n/a"
    clean_env.setenv("LLM_BACKEND", "claude")
    _block_all_http(monkeypatch)
    out = asyncio.run(llm.usage())
    assert out == "n/a", (
        f"AC4 — non-openrouter backend usage() must return 'n/a'; got {out!r}"
    )


def test_usage_makes_no_http_call_when_not_openrouter(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC4 — backend != openrouter → zero HTTP requests
    calls = _block_all_http(monkeypatch)
    asyncio.run(llm.usage())
    assert calls["count"] == 0, (
        f"AC4 — usage() must make zero HTTP calls when backend is not openrouter; "
        f"made {calls['count']}"
    )


def test_usage_openrouter_hits_auth_key_with_bearer(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC5 — GET https://openrouter.ai/api/v1/auth/key with Bearer header
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    response = httpx.Response(
        200,
        json={
            "data": {
                "usage": 0.012,
                "limit": 5.0,
                "rate_limit": {"requests": 200, "interval": "10s"},
            }
        },
    )
    captured = _patch_usage_response(monkeypatch, response)
    asyncio.run(llm.usage())

    assert captured["method"] == "GET", (
        f"AC5 — usage() must use GET; got {captured['method']!r}"
    )
    assert captured["url"] == "https://openrouter.ai/api/v1/auth/key", (
        f"AC5 — usage() must hit /api/v1/auth/key; got {captured['url']!r}"
    )
    assert captured["headers"].get("authorization") == "Bearer sk-test", (
        f"AC5 — usage() must carry Bearer header; got "
        f"{captured['headers'].get('authorization')!r}"
    )


def test_usage_openrouter_summary_contains_all_fields(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC5 — single-line summary surfaces usage, limit, rate
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    response = httpx.Response(
        200,
        json={
            "data": {
                "usage": 0.012,
                "limit": 5.0,
                "rate_limit": {"requests": 200, "interval": "10s"},
            }
        },
    )
    _patch_usage_response(monkeypatch, response)
    out = asyncio.run(llm.usage())

    assert "\n" not in out, (
        f"AC5 — usage() must return a single line; got multi-line {out!r}"
    )
    # Per AC5 the *fields* are pinned, not exact punctuation: usage dollars,
    # limit dollars, and rate (requests + interval) must each be present.
    assert "0.012" in out, f"AC5 — usage dollars must appear; got {out!r}"
    assert "5" in out, f"AC5 — limit dollars must appear; got {out!r}"
    assert "200" in out, f"AC5 — rate requests must appear; got {out!r}"
    assert "10s" in out, f"AC5 — rate interval must appear; got {out!r}"


def test_usage_openrouter_renders_null_limit_as_unlimited(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # edge — limit: null → "unlimited"
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    response = httpx.Response(
        200,
        json={
            "data": {
                "usage": 0.012,
                "limit": None,
                "rate_limit": {"requests": 200, "interval": "10s"},
            }
        },
    )
    _patch_usage_response(monkeypatch, response)
    out = asyncio.run(llm.usage())

    assert "unlimited" in out, (
        f"edge — null limit must render as literal 'unlimited'; got {out!r}"
    )


def test_usage_openrouter_returns_unavailable_on_non_2xx(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC6 — non-2xx → "unavailable (...)"
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    response = httpx.Response(503, text="upstream sad")
    _patch_usage_response(monkeypatch, response)
    out = asyncio.run(llm.usage())

    assert out.startswith("unavailable"), (
        f"AC6 — non-2xx must yield a string starting with 'unavailable'; got {out!r}"
    )


def test_usage_openrouter_returns_unavailable_on_error(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC6 — transport error → "unavailable (...)", does not raise
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_usage_response(monkeypatch, httpx.ConnectError("simulated"))

    out = asyncio.run(llm.usage())
    assert out.startswith("unavailable"), (
        f"AC6 — transport error must surface as 'unavailable (...)' not raise; "
        f"got {out!r}"
    )


def test_usage_openrouter_without_key_raises_runtimeerror(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # error — empty OPENROUTER_API_KEY propagates RuntimeError
    clean_env.setenv("LLM_BACKEND", "openrouter")
    # OPENROUTER_API_KEY left unset by clean_env fixture.
    _block_all_http(monkeypatch)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        asyncio.run(llm.usage())
