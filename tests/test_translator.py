"""Tests for translator pure helpers.

`translate` itself hits Google; we don't cover it here — bot.py treats the
library as a black box and catches failures.
"""

from __future__ import annotations

import pytest

import translator


def test_normalize_target_accepts_iso_code() -> None:
    assert translator.normalize_target("ru") == "ru"
    assert translator.normalize_target("EN") == "en"


def test_normalize_target_accepts_english_name() -> None:
    assert translator.normalize_target("russian") == "ru"
    assert translator.normalize_target("Russian") == "ru"
    assert translator.normalize_target("  SPANISH  ") == "es"


def test_normalize_target_rejects_empty() -> None:
    with pytest.raises(ValueError):
        translator.normalize_target("")
    with pytest.raises(ValueError):
        translator.normalize_target("   ")


def test_normalize_target_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        translator.normalize_target("klingon")
    with pytest.raises(ValueError):
        translator.normalize_target("zz")


def test_is_target_script_cyrillic_ru() -> None:
    assert translator.is_target_script("привет", "ru") is True
    assert translator.is_target_script("Привет, мир!", "ru") is True


def test_is_target_script_latin_input_rejects() -> None:
    assert translator.is_target_script("hello", "ru") is False
    assert translator.is_target_script("piece of cake", "ru") is False


def test_is_target_script_empty_or_punct_only() -> None:
    assert translator.is_target_script("", "ru") is False
    assert translator.is_target_script("   ", "ru") is False
    assert translator.is_target_script("!!! 123", "ru") is False


def test_is_target_script_mixed_script_goes_by_majority() -> None:
    # 5 Latin letters vs 6 Cyrillic → target (Cyrillic) wins.
    assert translator.is_target_script("hello привет", "ru") is True
    # Majority Latin → not reverse.
    assert translator.is_target_script("hello world привет", "ru") is False


def test_is_target_script_latin_target_never_reverses() -> None:
    # Can't distinguish German/French/Spanish from English by script alone,
    # so Latin targets always return False.
    assert translator.is_target_script("Hallo", "de") is False
    assert translator.is_target_script("Bonjour", "fr") is False
    assert translator.is_target_script("hola mundo", "es") is False


def test_is_target_script_other_scripts() -> None:
    assert translator.is_target_script("你好", "zh-CN") is True
    assert translator.is_target_script("こんにちは", "ja") is True
    assert translator.is_target_script("안녕하세요", "ko") is True
    assert translator.is_target_script("مرحبا", "ar") is True
    assert translator.is_target_script("Γειά σου", "el") is True


def test_is_target_script_unknown_target_returns_false() -> None:
    # A target we haven't mapped (shouldn't happen in practice, but guard).
    assert translator.is_target_script("привет", "xx") is False


def test_format_vocab_note_added() -> None:
    assert translator.format_vocab_note(1, added=True) == "added to vocab ✅"
    assert translator.format_vocab_note(5, added=True) == "added to vocab ✅"


def test_format_vocab_note_duplicate() -> None:
    assert translator.format_vocab_note(2, added=False) == "already in vocab"


def test_format_vocab_note_too_long() -> None:
    assert translator.format_vocab_note(6, added=False) == "not added (6 words)"
    # `added` is ignored once over the limit.
    assert translator.format_vocab_note(9, added=True) == "not added (9 words)"


def test_format_vocab_note_custom_max() -> None:
    assert translator.format_vocab_note(3, added=True, max_words=2) == "not added (3 words)"


def test_vocab_target_forward_picks_source() -> None:
    # English→target: the English source is what we want in vocab.
    assert translator.vocab_target("hello", "привет", reverse=False) == "hello"


def test_vocab_target_reverse_picks_translation() -> None:
    # target→English: the English translation is what we want in vocab.
    assert translator.vocab_target("привет", "hello", reverse=True) == "hello"


def test_pending_vocab_register_returns_unique_monotonic_tokens() -> None:
    pv = translator.PendingVocab()
    t1 = pv.register(111, "hello")
    t2 = pv.register(111, "world")
    t3 = pv.register(222, "hello")
    assert t1 != t2 != t3
    assert t2 > t1 and t3 > t2


def test_pending_vocab_pop_returns_entry_and_removes_it() -> None:
    pv = translator.PendingVocab()
    token = pv.register(42, "goodbye")
    assert pv.pop(token) == (42, "goodbye")
    # Second pop of the same token is a miss.
    assert pv.pop(token) is None


def test_pending_vocab_pop_unknown_returns_none() -> None:
    pv = translator.PendingVocab()
    assert pv.pop(999) is None


def test_pending_vocab_evicts_oldest_past_cap() -> None:
    pv = translator.PendingVocab(max_size=3)
    t1 = pv.register(1, "a")
    t2 = pv.register(1, "b")
    t3 = pv.register(1, "c")
    t4 = pv.register(1, "d")  # should evict t1
    assert pv.pop(t1) is None
    assert pv.pop(t2) == (1, "b")
    assert pv.pop(t3) == (1, "c")
    assert pv.pop(t4) == (1, "d")


def test_pending_vocab_len_tracks_live_entries() -> None:
    pv = translator.PendingVocab()
    assert len(pv) == 0
    t = pv.register(1, "a")
    assert len(pv) == 1
    pv.pop(t)
    assert len(pv) == 0
