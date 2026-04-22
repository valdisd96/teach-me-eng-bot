"""Tests for prompts.py."""

from __future__ import annotations

import random

import pytest

import prompts


def test_resolve_tone_passes_through_fixed() -> None:
    for t in prompts.TONES:
        assert prompts.resolve_tone(t) == t


def test_resolve_tone_expands_mixed_with_seed() -> None:
    r = random.Random(0)
    chosen = prompts.resolve_tone("mixed", rng=r)
    assert chosen in prompts.TONES


def test_resolve_tone_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        prompts.resolve_tone("whimsical")


def test_push_messages_has_system_and_user_with_word() -> None:
    msgs = prompts.push_messages("ephemeral", "funny")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "ephemeral" in msgs[1]["content"]
    # Push system prompt must demand literal appearance of the word.
    assert "literally" in msgs[0]["content"].lower()


def test_push_messages_mixed_tone_uses_rng() -> None:
    msgs = prompts.push_messages("placid", "mixed", rng=random.Random(0))
    # The system prompt contains one of the concrete tone instructions.
    sys = msgs[0]["content"]
    assert any(
        snippet[:20] in sys
        for snippet in [
            "Write a playful",
            "Write a warm",
            "Write a brief, eerie",
            "Write a vivid",
        ]
    )


def test_explain_messages_has_system_and_user_with_word() -> None:
    msgs = prompts.explain_messages("ephemeral")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "ephemeral" in msgs[1]["content"]
    # System prompt should steer toward a compact meaning + example.
    sys = msgs[0]["content"].lower()
    assert "example" in sys
    assert "two sentences" in sys


def test_just_talk_system_without_vocab_just_appends_length_rule() -> None:
    out = prompts.just_talk_system("You are helpful.", [])
    assert out.startswith("You are helpful.")
    assert "1–2 paragraphs" in out
    assert "vocabulary list" not in out


def test_just_talk_system_with_vocab_includes_words() -> None:
    out = prompts.just_talk_system("You are helpful.", ["ephemeral", "placid"])
    assert "ephemeral" in out and "placid" in out
    assert "vocabulary list" in out
