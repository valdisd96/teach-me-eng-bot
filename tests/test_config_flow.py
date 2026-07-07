"""Tests for config_flow.py — validators, session state machine, persistence."""

from __future__ import annotations

import sqlite3

import pytest

import config_flow


# --- validators --------------------------------------------------------------


def test_validate_tz_accepts_iana() -> None:
    assert config_flow.validate_tz("Europe/Warsaw") == "Europe/Warsaw"
    assert config_flow.validate_tz(" UTC ") == "UTC"


def test_validate_tz_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        config_flow.validate_tz("Middle/Earth")


def test_validate_words_accepts_range() -> None:
    for n in range(config_flow.MIN_WORDS, config_flow.MAX_WORDS + 1):
        assert config_flow.validate_words(str(n)) == n


def test_validate_words_rejects_out_of_range() -> None:
    below = str(config_flow.MIN_WORDS - 1)
    above = str(config_flow.MAX_WORDS + 1)
    for bad in (below, above, "0", "-1", "lots"):
        with pytest.raises(ValueError):
            config_flow.validate_words(bad)


def test_validate_hhmm_normalizes_to_two_digits() -> None:
    assert config_flow.validate_hhmm("9:00") == "09:00"
    assert config_flow.validate_hhmm("23:59") == "23:59"


def test_validate_hhmm_rejects_bad_format() -> None:
    for bad in ("24:00", "9", "nine", "9:60", "09-00"):
        with pytest.raises(ValueError):
            config_flow.validate_hhmm(bad)


def test_validate_tone_lowercases_and_checks() -> None:
    assert config_flow.validate_tone("Funny") == "funny"
    assert config_flow.validate_tone("MIXED") == "mixed"


def test_validate_tone_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        config_flow.validate_tone("whimsical")


# --- ConfigSession -----------------------------------------------------------


def _walk_happy_path(session: config_flow.ConfigSession) -> None:
    for reply in ("Europe/Warsaw", "7", "09:00", "21:00", "mixed", "russian"):
        advanced, _ = session.submit(reply)
        assert advanced is True


def test_session_completes_with_valid_replies() -> None:
    s = config_flow.ConfigSession()
    assert s.done is False
    _walk_happy_path(s)
    assert s.done is True
    settings = s.settings()
    assert settings.tz == "Europe/Warsaw"
    assert settings.words_per_day == 7
    assert settings.active_start == "09:00"
    assert settings.active_end == "21:00"
    assert settings.tone == "mixed"
    assert settings.target_lang == "ru"


def test_validate_target_lang_accepts_code_and_name() -> None:
    assert config_flow.validate_target_lang("ru") == "ru"
    assert config_flow.validate_target_lang("Russian") == "ru"


def test_validate_target_lang_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        config_flow.validate_target_lang("klingon")


def test_session_stays_on_step_on_invalid_input() -> None:
    s = config_flow.ConfigSession()
    advanced, reply = s.submit("Middle/Earth")
    assert advanced is False
    assert "timezone" in reply.lower()
    # Still on step 0
    assert s.idx == 0
    # Valid retry advances
    advanced, _ = s.submit("UTC")
    assert advanced is True
    assert s.idx == 1


def test_session_settings_raises_before_completion() -> None:
    s = config_flow.ConfigSession()
    with pytest.raises(RuntimeError):
        s.settings()


def test_session_submit_after_done_is_noop() -> None:
    s = config_flow.ConfigSession()
    _walk_happy_path(s)
    advanced, reply = s.submit("anything")
    assert advanced is False
    assert reply == config_flow.DONE_MESSAGE


# --- persistence -------------------------------------------------------------


def test_save_and_load_settings_roundtrip(conn: sqlite3.Connection) -> None:
    s = config_flow.Settings(
        tz="UTC",
        words_per_day=4,
        active_start="08:00",
        active_end="22:00",
        tone="funny",
        target_lang="ru",
    )
    config_flow.save_settings(conn, 123, s)
    loaded = config_flow.load_settings(conn, 123)
    assert loaded == s


def test_save_settings_upserts(conn: sqlite3.Connection) -> None:
    first = config_flow.Settings("UTC", 3, "09:00", "21:00", "mixed", "ru")
    second = config_flow.Settings("Europe/Warsaw", 5, "08:30", "23:00", "scary", "es")
    config_flow.save_settings(conn, 1, first)
    config_flow.save_settings(conn, 1, second)
    loaded = config_flow.load_settings(conn, 1)
    assert loaded == second
    count = conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]
    assert count == 1


def test_load_settings_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert config_flow.load_settings(conn, 999) is None
