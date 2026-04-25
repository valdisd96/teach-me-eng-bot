"""Tests for llm.py parse helpers, backend dispatch, and bench (no network)."""

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


def test_sse_delta_extracts_content() -> None:
    line = 'data: {"choices":[{"delta":{"content":"hello"}}]}'
    assert llm._parse_sse_delta(line) == "hello"


def test_sse_delta_returns_none_on_done() -> None:
    assert llm._parse_sse_delta("data: [DONE]") is None


def test_sse_delta_returns_none_on_non_data_line() -> None:
    assert llm._parse_sse_delta("event: ping") is None
    assert llm._parse_sse_delta("") is None


def test_sse_delta_tolerates_empty_delta() -> None:
    # Some llama.cpp builds emit {"delta":{}} for the opening chunk.
    line = 'data: {"choices":[{"delta":{}}]}'
    assert llm._parse_sse_delta(line) is None


def test_sse_delta_tolerates_junk_payload() -> None:
    assert llm._parse_sse_delta("data: not-json") is None
    assert llm._parse_sse_delta('data: {"choices":[]}') is None


def test_parse_completion_returns_content() -> None:
    payload = {"choices": [{"message": {"content": "paragraph here"}}]}
    assert llm._parse_completion(payload) == "paragraph here"


def test_parse_completion_missing_content_returns_empty() -> None:
    payload = {"choices": [{"message": {}}]}
    assert llm._parse_completion(payload) == ""


def test_bench_formats_chars_elapsed_and_rate() -> None:
    clock = iter([100.0, 102.0])  # 2 s elapsed

    async def fake_chat(messages, **kw):
        assert kw.get("max_tokens") == 32
        return "x" * 40  # 40 chars ≈ 10 tokens → 5 tok/s

    out = asyncio.run(llm.bench(chat_fn=fake_chat, now=lambda: next(clock)))
    assert out == "40 chars in 2.0s (~5.0 tok/s)"


def test_bench_timeout_returns_model_not_responding() -> None:
    async def hangs(messages, **kw):
        await asyncio.sleep(10)
        return "unreachable"

    out = asyncio.run(llm.bench(chat_fn=hangs, timeout=0.05))
    assert out == "model not responding"


def test_bench_error_reports_exception_type() -> None:
    async def boom(messages, **kw):
        raise ConnectionError("nope")

    out = asyncio.run(llm.bench(chat_fn=boom))
    assert out == "error: ConnectionError"


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


# stream_chat() request shape -------------------------------------------


def _patch_stream(monkeypatch: pytest.MonkeyPatch, sse_body: bytes) -> dict:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def _collect_stream(messages: list[dict]) -> list[str]:
    async def run() -> list[str]:
        return [d async for d in llm.stream_chat(messages)]

    return asyncio.run(run())


def test_stream_chat_default_targets_llama(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    captured = _patch_stream(monkeypatch, sse)
    deltas = _collect_stream([{"role": "user", "content": "hi"}])
    assert deltas == ["hi"]
    assert captured["url"] == llm.LLAMA_URL
    assert "authorization" not in captured["headers"]
    assert captured["body"]["stream"] is True


def test_stream_chat_openrouter_carries_bearer(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("LLM_BACKEND", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "k")
    sse = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'
    captured = _patch_stream(monkeypatch, sse)
    deltas = _collect_stream([{"role": "user", "content": "hi"}])
    assert deltas == ["x"]
    assert captured["url"] == llm.OPENROUTER_URL
    assert captured["headers"]["authorization"] == "Bearer k"


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
