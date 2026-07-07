"""Story-result 💡 explain buttons + the retired on_rate stub.

The daily cloze story replaced the multi-push format: the final story-result
message now carries one `💡 <word>` inline button per missed word
(callback `exp:<word_id>` → `bot.on_explain` → `compose_explanation`),
and taps on legacy ✅/❌ push buttons hit a stub (`bot.on_rate`) that only
clears the buttons — no rating, no DB writes.

Mocking shape mirrors `tests/test_games_cancel.py`: Telegram Update / Context
are MagicMock + AsyncMock at the architectural seam, with `bot.conn` patched
to the temp-DB fixture from conftest.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import cloze  # noqa: E402
import scheduler  # noqa: E402
import vocab  # noqa: E402


CHAT = 30500


# -- helpers -----------------------------------------------------------------


def _make_update(chat_id: int = CHAT, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(data: str, chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _seed_chat(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, active_start, "
        "active_end, tone, created_at) "
        "VALUES (?, 'UTC', 6, '00:00', '00:00', 'mixed', '2026-01-01')",
        (CHAT,),
    )


def _add_word(conn: sqlite3.Connection, text: str) -> int:
    vocab.add_word(conn, CHAT, text)
    wid = vocab.find_word_id(conn, CHAT, text)
    assert wid is not None
    return wid


def _session(
    words: list[tuple[int, str]], *, push_id: int | None = None
) -> cloze.Session:
    story = " and ".join(f"a {w}" for _, w in words) + "."
    display, order, missing = cloze.blank_story(story, [w for _, w in words])
    assert not missing
    blanks = [
        cloze.Blank(word_id=words[i][0], word=words[i][1], is_intro=False)
        for i in order
    ]
    return cloze.Session(
        chat_id=CHAT, story=story, display=display, blanks=blanks, push_id=push_id
    )


def _seeded_session(conn: sqlite3.Connection, words: list[str]) -> cloze.Session:
    _seed_chat(conn)
    ids = [_add_word(conn, w) for w in words]
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=ids)
    s = _session(list(zip(ids, words)), push_id=push_id)
    bot.cloze_sessions[CHAT] = s
    return s


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "append_turn", lambda *a, **k: None)


def _explain_button_callbacks(reply_markup) -> set[str]:
    if reply_markup is None:
        return set()
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return set()
    return {b.callback_data for row in rows for b in row}


# === bot.on_explain ==========================================================


def test_on_explain_replies_word_and_explanation_html(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # happy path — 💡 <b>word</b> + explanation, parse_mode HTML
    _patch_bot(monkeypatch, conn)
    _seed_chat(conn)
    word_id = _add_word(conn, "ephemeral")
    monkeypatch.setattr(
        bot.sched_module,
        "compose_explanation",
        AsyncMock(return_value="It means X. Example."),
    )

    update = _make_callback_update(f"exp:{word_id}")
    asyncio.run(bot.on_explain(update, _make_context()))

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    call = update.callback_query.message.reply_text.call_args
    text = call.args[0] if call.args else call.kwargs["text"]
    assert "ephemeral" in text, f"reply must name the word; got {text!r}"
    assert "It means X. Example." in text, (
        f"reply must carry the explanation; got {text!r}"
    )
    assert text.startswith("💡"), f"reply must lead with the 💡 marker; got {text!r}"
    assert call.kwargs.get("parse_mode") == "HTML", (
        f"reply must be sent with parse_mode='HTML'; got {call.kwargs!r}"
    )


def test_on_explain_failure_sends_warning(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # compose_explanation raises → soft ⚠️ reply, no crash
    _patch_bot(monkeypatch, conn)
    _seed_chat(conn)
    word_id = _add_word(conn, "ephemeral")
    monkeypatch.setattr(
        bot.sched_module,
        "compose_explanation",
        AsyncMock(side_effect=RuntimeError("llm down")),
    )

    update = _make_callback_update(f"exp:{word_id}")
    asyncio.run(bot.on_explain(update, _make_context()))

    update.callback_query.message.reply_text.assert_awaited_once()
    call = update.callback_query.message.reply_text.call_args
    text = call.args[0] if call.args else call.kwargs["text"]
    assert text.startswith("⚠️"), (
        f"a failed explanation must produce the ⚠️ fallback reply; got {text!r}"
    )


# === bot.on_rate (retired stub) ==============================================


def test_on_rate_stub_clears_markup_without_db_writes(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # legacy ✅/❌ tap → answer + markup cleared, no rating
    _patch_bot(monkeypatch, conn)
    _seed_chat(conn)
    word_id = _add_word(conn, "ephemeral")
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=[word_id])
    reps_before = conn.execute(
        "SELECT reps FROM words WHERE id = ?", (word_id,)
    ).fetchone()["reps"]

    update = _make_callback_update(f"rate:good:{push_id}:{word_id}")
    asyncio.run(bot.on_rate(update, _make_context()))

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(None)
    reps_after = conn.execute(
        "SELECT reps FROM words WHERE id = ?", (word_id,)
    ).fetchone()["reps"]
    assert reps_after == reps_before, (
        "the retired on_rate stub must not rate the word — reps changed from "
        f"{reps_before} to {reps_after}"
    )


# === story completion — 💡 buttons on the result message ====================


def test_story_result_attaches_explain_buttons_for_missed_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # one miss → one 💡 button with exp:<missed_word_id>
    _patch_bot(monkeypatch, conn)
    s = _seeded_session(conn, ["horse", "potent"])
    missed_word_id = s.blanks[0].word_id
    other_word = s.blanks[1].word

    # Miss blank 1 by typing the OTHER bank word (a real answer, graded wrong).
    asyncio.run(
        bot.handle_message(_make_update(text=other_word), _make_context())
    )
    # Answer the final blank correctly — this completes the session.
    ctx = _make_context()
    asyncio.run(
        bot.handle_message(_make_update(text=s.blanks[1].word), ctx)
    )

    assert s.done, "two answers must complete a 2-blank session"
    ctx.bot.send_message.assert_awaited_once()
    kb = ctx.bot.send_message.call_args.kwargs.get("reply_markup")
    cbs = _explain_button_callbacks(kb)
    assert f"exp:{missed_word_id}" in cbs, (
        f"the result message must carry a 💡 button for the missed word; got {cbs!r}"
    )
    assert f"exp:{s.blanks[1].word_id}" not in cbs, (
        "correctly answered words must not get an explain button"
    )


def test_story_result_all_correct_has_no_keyboard(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # zero misses → reply_markup=None on the result message
    _patch_bot(monkeypatch, conn)
    s = _seeded_session(conn, ["horse", "potent"])

    asyncio.run(
        bot.handle_message(_make_update(text=s.blanks[0].word), _make_context())
    )
    ctx = _make_context()
    asyncio.run(
        bot.handle_message(_make_update(text=s.blanks[1].word), ctx)
    )

    assert s.done and s.score == 2
    ctx.bot.send_message.assert_awaited_once()
    assert ctx.bot.send_message.call_args.kwargs.get("reply_markup") is None, (
        "an all-correct session must send the result without an inline keyboard"
    )
