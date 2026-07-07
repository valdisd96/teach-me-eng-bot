"""Spell-check on /add — a typo baked into vocab poisons every future story.

Pure `spelling.suggest` tests hit the real dictionary once (lazy-loaded);
bot-level tests monkeypatch `spelling.suggest` so they stay hermetic, and
mock Telegram at the seam like tests/test_games_cancel.py.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402
import spelling  # noqa: E402
import vocab  # noqa: E402


CHAT = 40600


# --- spelling.suggest (real dictionary) ---------------------------------------


def test_suggest_corrects_the_production_typo() -> None:
    assert spelling.suggest("humiliationg") == "humiliating"


def test_suggest_known_word_returns_none() -> None:
    assert spelling.suggest("ultrasound") is None
    assert spelling.suggest("Sniveling") is None  # case-insensitive


def test_suggest_skips_phrases_and_nonalpha() -> None:
    assert spelling.suggest("run out of") is None
    assert spelling.suggest("covid-19") is None
    assert spelling.suggest("") is None


def test_suggest_unknown_without_candidate_returns_none() -> None:
    assert spelling.suggest("qzxvbnmt") is None


# --- bot: /add wiring -----------------------------------------------------------


def _make_update(text_args: str) -> tuple[MagicMock, MagicMock]:
    update = MagicMock()
    update.effective_chat.id = CHAT
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = text_args.split()
    return update, ctx


def _patch(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(
        bot.translator, "translate", lambda *a, **k: "перевод"
    )


def test_add_typo_offers_buttons_and_does_not_add(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    monkeypatch.setattr(bot.spelling, "suggest", lambda w: "humiliating")
    update, ctx = _make_update("humiliationg")

    asyncio.run(bot.cmd_add(update, ctx))

    assert vocab.find_word_id(conn, CHAT, "humiliationg") is None, (
        "the typo must NOT be added until the user confirms"
    )
    call = update.message.reply_text.call_args
    assert "did you mean" in call.args[0]
    kb = call.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("humiliating" in lbl for lbl in labels)
    assert any("humiliationg" in lbl for lbl in labels)
    # Both buttons are av: callbacks backed by the pending-vocab registry.
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(d.startswith("av:") for d in datas)


def test_add_suggestion_button_adds_corrected_word(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    monkeypatch.setattr(bot.spelling, "suggest", lambda w: "humiliating")
    update, ctx = _make_update("humiliationg")
    asyncio.run(bot.cmd_add(update, ctx))
    kb = update.message.reply_text.call_args.kwargs["reply_markup"]
    fixed_data = kb.inline_keyboard[0][0].callback_data

    cb_update = MagicMock()
    cb_update.effective_chat.id = CHAT
    cb_update.effective_user.id = 1
    cb_update.effective_user.is_bot = False
    cb_update.callback_query.data = fixed_data
    cb_update.callback_query.answer = AsyncMock()
    cb_update.callback_query.edit_message_reply_markup = AsyncMock()
    asyncio.run(bot.on_add_vocab(cb_update, MagicMock()))

    assert vocab.find_word_id(conn, CHAT, "humiliating") is not None
    assert vocab.find_word_id(conn, CHAT, "humiliationg") is None


def test_add_clean_word_unaffected(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    monkeypatch.setattr(bot.spelling, "suggest", lambda w: None)
    update, ctx = _make_update("ultrasound")

    asyncio.run(bot.cmd_add(update, ctx))

    assert vocab.find_word_id(conn, CHAT, "ultrasound") is not None
    assert "➕ Added: ultrasound" in update.message.reply_text.call_args.args[0]


def test_add_existing_word_skips_spellcheck(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "humiliationg")

    def _boom(word: str) -> str | None:
        raise AssertionError("spell-check must not run for existing words")

    monkeypatch.setattr(bot.spelling, "suggest", _boom)
    update, ctx = _make_update("humiliationg")

    asyncio.run(bot.cmd_add(update, ctx))

    assert "Already in your vocab" in update.message.reply_text.call_args.args[0]


def test_add_spellcheck_failure_falls_through(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)

    def _boom(word: str) -> str | None:
        raise RuntimeError("dictionary unavailable")

    monkeypatch.setattr(bot.spelling, "suggest", _boom)
    update, ctx = _make_update("weirdword")

    asyncio.run(bot.cmd_add(update, ctx))

    assert vocab.find_word_id(conn, CHAT, "weirdword") is not None, (
        "a broken spell-checker must never block adding words"
    )
