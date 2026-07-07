"""Just-talk one-shot flow — the streaming live-edit machinery is gone.

Mocking mirrors tests/test_games_cancel.py: Update/Context are MagicMock +
AsyncMock at the seam, `bot.conn` patched to the conftest temp DB, and
`llm.chat` monkeypatched so no HTTP happens.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402
import llm  # noqa: E402
import vocab  # noqa: E402


CHAT = 30500


def _make_update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = CHAT
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.args = []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def _patch(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "append_turn", lambda *a, **k: None)


def test_just_talk_sends_one_shot_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    chat_spy = AsyncMock(return_value="Nice to meet you!")
    monkeypatch.setattr(llm, "chat", chat_spy)
    update = _make_update("hello there")
    ctx = _make_context()

    asyncio.run(bot.handle_message(update, ctx))

    ctx.bot.send_chat_action.assert_awaited_once()
    chat_spy.assert_awaited_once()
    sent = ctx.bot.send_message.call_args.kwargs
    assert "Nice to meet you!" in sent["text"]
    # History carries system + user + assistant.
    roles = [m["role"] for m in bot.histories[CHAT]]
    assert roles == ["system", "user", "assistant"]


def test_just_talk_reply_highlights_vocab(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    monkeypatch.setattr(
        llm, "chat", AsyncMock(return_value="Life is ephemeral indeed.")
    )
    update = _make_update("tell me something")
    ctx = _make_context()

    asyncio.run(bot.handle_message(update, ctx))

    sent = ctx.bot.send_message.call_args.kwargs
    assert "<code>ephemeral</code>" in sent["text"]
    assert sent["parse_mode"] == "HTML"
    # The literal mention bumps mention_count.
    row = conn.execute(
        "SELECT mention_count FROM words WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    assert row["mention_count"] == 1


def test_just_talk_long_reply_spills_messages(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    long_reply = "word " * 1700  # ~8500 chars > 2 * MAX_MSG_LEN
    monkeypatch.setattr(llm, "chat", AsyncMock(return_value=long_reply))
    update = _make_update("talk a lot")
    ctx = _make_context()

    asyncio.run(bot.handle_message(update, ctx))

    assert ctx.bot.send_message.await_count >= 3
    for call in ctx.bot.send_message.call_args_list:
        assert len(call.kwargs["text"]) <= bot.MAX_MSG_LEN + 100  # HTML slack


def test_just_talk_error_reports_to_user(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, conn)
    monkeypatch.setattr(
        llm, "chat", AsyncMock(side_effect=RuntimeError("backend down"))
    )
    update = _make_update("hello?")
    ctx = _make_context()

    asyncio.run(bot.handle_message(update, ctx))

    reply = update.message.reply_text.call_args.args[0]
    assert reply.startswith("⚠️ Error:")
    # The failed turn must not leave a dangling assistant message.
    roles = [m["role"] for m in bot.histories[CHAT]]
    assert roles == ["system", "user"]


def test_streaming_machinery_is_gone() -> None:
    assert not hasattr(llm, "stream_chat")
    assert not hasattr(bot, "safe_edit")
    assert not hasattr(bot, "CURSOR")
