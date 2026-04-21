"""Tests for llm.py parse helpers (no network)."""

from __future__ import annotations

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
