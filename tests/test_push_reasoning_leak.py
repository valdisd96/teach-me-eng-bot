"""Scheduled pushes and ❌-forgot explanations must disable model reasoning.

The openrouter/free auto-router sometimes routes these calls to reasoning
models whose chain-of-thought either consumes the whole max_tokens budget
(content: null) or comes back as the content itself, so the user receives
"We need to insert the word ..." traces instead of a snippet. `llm.chat`
already grew a `disable_reasoning` kwarg for this exact failure (#100); these
tests pin that the push/explain call sites actually use it.

Telegram Update/Context is mocked at the seam (AsyncMock + MagicMock) per
the project convention (see `tests/test_add_no_label_suggester.py`).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import config_flow  # noqa: E402
import llm  # noqa: E402
import vocab  # noqa: E402


CHAT = 424242


def _seed_chat(conn: sqlite3.Connection) -> None:
    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings("UTC", 2, "09:00", "21:00", "funny", "ru"),
    )


def _capture_llm_chat(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[dict]:
    """Replace llm.chat with a fake that records every call's kwargs."""
    calls: list[dict] = []

    async def fake_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return reply

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def test_push_llm_chat_disables_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_llm_chat(monkeypatch, "a snippet")
    msgs = [{"role": "user", "content": "hi"}]

    out = asyncio.run(bot._push_llm_chat(msgs))

    assert out == "a snippet"
    assert calls == [{"messages": msgs, "disable_reasoning": True}]


def test_dispatch_push_disables_reasoning(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    calls = _capture_llm_chat(monkeypatch, "such an ephemeral moment")

    app = MagicMock()
    msg = MagicMock()
    msg.message_id = 777
    app.bot.send_message = AsyncMock(return_value=msg)
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "app", app)
    monkeypatch.setattr(bot, "append_turn", lambda *a, **k: None)

    asyncio.run(bot.dispatch_push(CHAT))

    app.bot.send_message.assert_awaited_once()
    assert calls, "dispatch_push must reach the LLM"
    assert all(c.get("disable_reasoning") is True for c in calls)


def test_on_rate_explanation_disables_reasoning(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    row = conn.execute(
        "SELECT id FROM words WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    word_id = row["id"]
    push_id = bot.sched_module.log_push(
        conn, CHAT, tg_message_id=1, word_ids=[word_id]
    )
    calls = _capture_llm_chat(monkeypatch, "lasting a very short time")

    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "append_turn", lambda *a, **k: None)
    # ❌-forgot follow-ups also translate the word — not under test here.
    monkeypatch.setattr(
        bot.translator, "translate", lambda *a, **k: "эфемерный", raising=False
    )

    update = MagicMock()
    update.effective_chat.id = CHAT
    update.callback_query.data = f"rate:again:{push_id}:{word_id}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()

    asyncio.run(bot.on_rate(update, ctx))

    assert calls, "the ❌-forgot explanation must reach the LLM"
    assert all(c.get("disable_reasoning") is True for c in calls)
