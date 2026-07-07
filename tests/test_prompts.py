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
    assert any(instr in sys for instr in prompts._TONE_INSTRUCTIONS.values())


def test_push_messages_demand_simple_learner_english() -> None:
    # Pushes must steer toward plain, predictable learner-level snippets whose
    # context makes the target word's meaning guessable.
    sys = prompts.push_messages("ephemeral", "funny")[0]["content"].lower()
    assert "simple, everyday english" in sys
    assert "a2" in sys
    assert "guess its meaning from the context" in sys
    assert "25 words" in sys


def test_tone_instructions_stay_plain() -> None:
    # Tone flavour must not reintroduce florid prose — every instruction is
    # anchored in an everyday register or an explicitly plain style.
    for tone, instr in prompts._TONE_INSTRUCTIONS.items():
        assert "everyday" in instr or "friendly" in instr, (
            f"tone {tone!r} instruction drifted from plain style: {instr!r}"
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


# -- grade_translation_messages (#143 — AC7) ---------------------------------


def test_grade_translation_messages_two_dicts_system_user() -> None:
    # AC7 — exactly 2 dicts; system first, user second.
    msgs = prompts.grade_translation_messages("cat", "кошка", "кот")
    assert isinstance(msgs, list), f"AC7 — must return a list, got {type(msgs).__name__}"
    assert len(msgs) == 2, f"AC7 — must return exactly 2 dicts, got {len(msgs)}"
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user"], (
        f"AC7 — element 0 must be system, element 1 must be user; got {roles}"
    )


def test_grade_translation_messages_user_contains_inputs() -> None:
    # AC7 — user-role content embeds prompt, expected, user_text as literal substrings.
    p, e, u = "to get tired of", "уставший от", "устать"
    msgs = prompts.grade_translation_messages(p, e, u)
    user_content = msgs[1]["content"]
    assert p in user_content, (
        f"AC7 — prompt {p!r} must appear literally in user content; got {user_content!r}"
    )
    assert e in user_content, (
        f"AC7 — expected {e!r} must appear literally in user content; got {user_content!r}"
    )
    assert u in user_content, (
        f"AC7 — user_text {u!r} must appear literally in user content; got {user_content!r}"
    )


def test_grade_translation_messages_system_has_yes_no_rubric() -> None:
    # AC7 — system content carries a YES/NO rubric.
    msgs = prompts.grade_translation_messages("cat", "кошка", "кот")
    sys_content = msgs[0]["content"]
    assert "YES" in sys_content, (
        f"AC7 — system rubric must mention YES verdict; got {sys_content!r}"
    )
    assert "NO" in sys_content, (
        f"AC7 — system rubric must mention NO verdict; got {sys_content!r}"
    )
