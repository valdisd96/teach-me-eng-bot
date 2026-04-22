"""Tests for translator.normalize_target.

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
