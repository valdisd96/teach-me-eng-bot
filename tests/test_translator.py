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


def test_format_reverse_note_added() -> None:
    assert translator.format_reverse_note(1, added=True) == "added to vocab ✅"
    assert translator.format_reverse_note(5, added=True) == "added to vocab ✅"


def test_format_reverse_note_duplicate() -> None:
    assert translator.format_reverse_note(2, added=False) == "already in vocab"


def test_format_reverse_note_too_long() -> None:
    assert translator.format_reverse_note(6, added=False) == "not added (6 words)"
    # `added` is ignored once over the limit.
    assert translator.format_reverse_note(9, added=True) == "not added (9 words)"


def test_format_reverse_note_custom_max() -> None:
    assert translator.format_reverse_note(3, added=True, max_words=2) == "not added (3 words)"
