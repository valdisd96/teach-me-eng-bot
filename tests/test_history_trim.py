"""Tests for bot.trim_history — the just-talk history cap.

Pure helper: histories longer than MAX_HISTORY_MESSAGES (1 system + 40
turns) are trimmed to the system message plus the most recent turns, so the
LLM context can't grow unbounded over a long-running chat.
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)


def _history(n_turns: int) -> list[dict]:
    return [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
        for i in range(n_turns)
    ]


def test_max_history_messages_constant() -> None:
    assert bot.MAX_HISTORY_MESSAGES == 41, (
        f"cap must be 41 (1 system + 40 turns); got {bot.MAX_HISTORY_MESSAGES}"
    )


def test_long_history_trimmed_to_system_plus_last_forty() -> None:
    history = _history(100)
    out = bot.trim_history(history)
    assert len(out) == bot.MAX_HISTORY_MESSAGES == 41
    assert out[0] is history[0], "the system message must survive at index 0"
    assert out[1:] == history[-40:], (
        "the trimmed tail must be exactly the most recent 40 turns"
    )


def test_short_history_returned_unchanged() -> None:
    history = _history(10)
    assert bot.trim_history(history) is history, (
        "histories within the cap must be returned unchanged"
    )


def test_exactly_at_cap_returned_unchanged() -> None:
    history = _history(40)  # 41 messages total == MAX_HISTORY_MESSAGES
    assert bot.trim_history(history) is history


def test_one_over_cap_drops_oldest_turn() -> None:
    history = _history(41)  # 42 messages total
    out = bot.trim_history(history)
    assert len(out) == 41
    assert out[0]["role"] == "system"
    # The oldest non-system turn ("0") is the one dropped.
    assert out[1]["content"] == "1"
    assert out[-1]["content"] == "40"


def test_custom_cap_respected() -> None:
    history = _history(10)  # 11 messages
    out = bot.trim_history(history, max_messages=5)
    assert len(out) == 5
    assert out[0]["role"] == "system"
    assert [m["content"] for m in out[1:]] == ["6", "7", "8", "9"]
