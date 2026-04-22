"""Tests for llm.py parse helpers and bench (no network)."""

from __future__ import annotations

import asyncio

import llm


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
