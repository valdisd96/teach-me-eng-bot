"""Tests for issue #76 — 🎮 Play game button under the ❌-forgot explanation.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue:
- on_rate, when verdict=again AND compose_explanation returns a non-empty
  string, attaches a one-button keyboard ("🎮 Play game", callback_data
  "pg:start") to the explanation reply.
- on_rate, when verdict=good OR explanation is None, does NOT attach the
  play-game button.
- on_play_game opens the same direction-picker `cmd_games` posts
  (gm:wt / gm:tw), gated by the same MIN_VOCAB and in-progress checks.
- A CallbackQueryHandler is registered for pattern `^pg:`.

Telegram Update/Context is mocked at the seam (AsyncMock + MagicMock); the
project's other handler tests stop at static surfaces, but every AC here is
dispatch-level so the mocks are unavoidable.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import games as games_module  # noqa: E402
import scheduler as sched_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 4242


# -- helpers -----------------------------------------------------------------


def _seed_chat_with_words(conn: sqlite3.Connection, n: int) -> list[int]:
    """Insert `n` translatable words for CHAT and return their word_ids."""
    vocab.ensure_chat(conn, CHAT)
    words = [f"word{i}" for i in range(1, n + 1)]
    translations = [f"trans{i}" for i in range(1, n + 1)]
    vocab.add_words_bulk(conn, CHAT, words, translations=translations)
    rows = conn.execute(
        "SELECT id FROM words WHERE chat_id = ? ORDER BY id", (CHAT,)
    ).fetchall()
    return [r["id"] for r in rows]


def _make_callback_update(callback_data: str, chat_id: int = CHAT) -> MagicMock:
    """Build a MagicMock Update with an async callback_query/message surface."""
    update = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    return update


def _patch_bot_runtime(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    *,
    explanation: str | None = "the explanation",
    translation: str | None = None,
) -> dict[str, MagicMock]:
    """Wire bot.conn + the scheduler/translator/llm seams used by on_rate.

    Returns a dict of the spies installed so tests can inspect call args.
    """
    monkeypatch.setattr(bot, "conn", conn)

    # bot.games is a module-level dict; reset between tests so AC4/AC5 are
    # not order-dependent.
    bot.games.clear()

    explanation_spy = AsyncMock(return_value=explanation)
    translation_spy = AsyncMock(return_value=translation)
    format_spy = MagicMock(side_effect=lambda body, t: body if t is None else f"{body}\n\n{t}")
    mark_rated_spy = MagicMock()
    monkeypatch.setattr(bot.sched_module, "compose_explanation", explanation_spy)
    monkeypatch.setattr(bot.sched_module, "compose_translation", translation_spy)
    monkeypatch.setattr(bot.sched_module, "format_explanation_reply", format_spy)
    monkeypatch.setattr(bot.sched_module, "mark_rated", mark_rated_spy)

    return {
        "explanation": explanation_spy,
        "translation": translation_spy,
        "format": format_spy,
        "mark_rated": mark_rated_spy,
    }


def _seed_push(conn: sqlite3.Connection, word_id: int) -> int:
    return sched_module.log_push(
        conn, CHAT, tg_message_id=None, word_ids=[word_id]
    )


def _play_game_buttons(reply_markup) -> list:
    """Flatten reply_markup.inline_keyboard rows and return all buttons."""
    if reply_markup is None:
        return []
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return []
    return [btn for row in rows for btn in row]


def _has_play_game_button(reply_markup) -> bool:
    return any(
        btn.text == "🎮 Play game" and btn.callback_data == "pg:start"
        for btn in _play_game_buttons(reply_markup)
    )


# -- AC1 — explanation reply carries the play-game button --------------------


def test_play_game_button_label_and_callback_data(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — button text == "🎮 Play game", callback_data == "pg:start"
    _patch_bot_runtime(monkeypatch, conn, explanation="meaning + example")
    [word_id] = _seed_chat_with_words(conn, 1)
    push_id = _seed_push(conn, word_id)
    update = _make_callback_update(f"rate:again:{push_id}:{word_id}")

    asyncio.run(bot.on_rate(update, MagicMock()))

    # The explanation reply must be the one carrying the button.
    play_calls = [
        c
        for c in update.callback_query.message.reply_text.call_args_list
        if _has_play_game_button(c.kwargs.get("reply_markup"))
    ]
    assert len(play_calls) == 1, (
        f"expected exactly one reply with the play-game button, "
        f"got {len(play_calls)} of "
        f"{update.callback_query.message.reply_text.call_args_list}"
    )
    btns = _play_game_buttons(play_calls[0].kwargs["reply_markup"])
    assert len(btns) == 1, f"play-game keyboard should have 1 button, got {btns}"
    assert btns[0].text == "🎮 Play game"
    assert btns[0].callback_data == "pg:start"


def test_on_rate_again_with_explanation_attaches_play_game_button(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — verdict=again + non-empty explanation → button on reply
    _patch_bot_runtime(monkeypatch, conn, explanation="real explanation text")
    [word_id] = _seed_chat_with_words(conn, 1)
    push_id = _seed_push(conn, word_id)
    update = _make_callback_update(f"rate:again:{push_id}:{word_id}")

    asyncio.run(bot.on_rate(update, MagicMock()))

    sent = update.callback_query.message.reply_text.call_args_list
    assert any(
        _has_play_game_button(c.kwargs.get("reply_markup")) for c in sent
    ), f"verdict=again must attach the play-game button to the explanation; got {sent}"


# -- AC2 — verdict=good never sends the play-game button ---------------------


def test_on_rate_good_does_not_send_play_game_button(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — verdict=good → no explanation, no play-game button
    spies = _patch_bot_runtime(monkeypatch, conn, explanation="should not be called")
    [word_id] = _seed_chat_with_words(conn, 1)
    push_id = _seed_push(conn, word_id)
    update = _make_callback_update(f"rate:good:{push_id}:{word_id}")

    asyncio.run(bot.on_rate(update, MagicMock()))

    sent = update.callback_query.message.reply_text.call_args_list
    assert not any(
        _has_play_game_button(c.kwargs.get("reply_markup")) for c in sent
    ), f"verdict=good must never attach the play-game button; got {sent}"
    # The rating-keyboard edit + mark_rated are part of "still happen as today".
    assert spies["mark_rated"].called, "mark_rated should still run on verdict=good"


# -- AC3 — explanation None → no play-game button ----------------------------


def test_on_rate_again_when_explanation_none_sends_no_play_button(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — verdict=again + compose_explanation None → no button
    _patch_bot_runtime(monkeypatch, conn, explanation=None)
    [word_id] = _seed_chat_with_words(conn, 1)
    push_id = _seed_push(conn, word_id)
    update = _make_callback_update(f"rate:again:{push_id}:{word_id}")

    asyncio.run(bot.on_rate(update, MagicMock()))

    sent = update.callback_query.message.reply_text.call_args_list
    assert not any(
        _has_play_game_button(c.kwargs.get("reply_markup")) for c in sent
    ), (
        "when compose_explanation returns None, no follow-up message — and "
        f"therefore no play-game button — should be sent; got {sent}"
    )


# -- AC4 — on_play_game opens direction picker -------------------------------


def test_on_play_game_with_enough_vocab_sends_direction_picker(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — vocab >= MIN_VOCAB AND no game → gm:wt / gm:tw menu
    _patch_bot_runtime(monkeypatch, conn)
    _seed_chat_with_words(conn, games_module.MIN_VOCAB)
    update = _make_callback_update("pg:start")

    asyncio.run(bot.on_play_game(update, MagicMock()))

    update.callback_query.message.reply_text.assert_called_once()
    call = update.callback_query.message.reply_text.call_args
    btns = _play_game_buttons(call.kwargs.get("reply_markup"))
    assert len(btns) == 2, (
        f"expected 2 direction-picker buttons, got {len(btns)}: {btns}"
    )
    by_data = {b.callback_data: b.text for b in btns}
    assert by_data.get("gm:wt") == "Word → Translation", (
        f"missing or mislabeled gm:wt button; got {by_data}"
    )
    assert by_data.get("gm:tw") == "Translation → Word", (
        f"missing or mislabeled gm:tw button; got {by_data}"
    )


# -- AC5 — in-progress short-circuit -----------------------------------------


def test_on_play_game_with_active_game_replies_in_progress(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — chat already in bot.games → GAMES_IN_PROGRESS, no menu
    _patch_bot_runtime(monkeypatch, conn)
    _seed_chat_with_words(conn, games_module.MIN_VOCAB)
    bot.games[CHAT] = MagicMock()  # simulate an in-flight game
    update = _make_callback_update("pg:start")

    asyncio.run(bot.on_play_game(update, MagicMock()))

    update.callback_query.message.reply_text.assert_called_once()
    call = update.callback_query.message.reply_text.call_args
    sent_text = call.args[0] if call.args else call.kwargs.get("text")
    assert sent_text == bot.GAMES_IN_PROGRESS, (
        f"expected GAMES_IN_PROGRESS literal, got {sent_text!r}"
    )
    # Crucially: no direction-picker menu surfaced.
    btns = _play_game_buttons(call.kwargs.get("reply_markup"))
    assert not any(b.callback_data in ("gm:wt", "gm:tw") for b in btns), (
        f"in-progress reply must NOT carry the direction-picker; got {btns}"
    )


# -- AC6 — too few words → need-vocab refusal --------------------------------


def test_on_play_game_with_too_few_vocab_replies_need_vocab(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — translatable rows < MIN_VOCAB → GAMES_NEED_VOCAB, no menu
    _patch_bot_runtime(monkeypatch, conn)
    _seed_chat_with_words(conn, games_module.MIN_VOCAB - 1)
    update = _make_callback_update("pg:start")

    asyncio.run(bot.on_play_game(update, MagicMock()))

    update.callback_query.message.reply_text.assert_called_once()
    call = update.callback_query.message.reply_text.call_args
    sent_text = call.args[0] if call.args else call.kwargs.get("text")
    assert sent_text == bot.GAMES_NEED_VOCAB, (
        f"expected GAMES_NEED_VOCAB literal, got {sent_text!r}"
    )
    btns = _play_game_buttons(call.kwargs.get("reply_markup"))
    assert not any(b.callback_data in ("gm:wt", "gm:tw") for b in btns), (
        f"need-vocab reply must NOT carry the direction-picker; got {btns}"
    )


# -- AC7 — query.answer() clears the spinner ---------------------------------


def test_on_play_game_calls_query_answer_before_replying(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — query.answer() must be called
    _patch_bot_runtime(monkeypatch, conn)
    _seed_chat_with_words(conn, games_module.MIN_VOCAB)
    update = _make_callback_update("pg:start")

    asyncio.run(bot.on_play_game(update, MagicMock()))

    update.callback_query.answer.assert_called_once()


# -- AC8 — handler registered with pattern ^pg: ------------------------------


def test_on_play_game_handler_registered_with_pg_pattern() -> None:  # AC8 — main() wires the handler
    src = Path(bot.__file__).read_text(encoding="utf-8")
    # The exact line plan-exec adds in main(); pattern can be quoted with " or '.
    assert (
        'CallbackQueryHandler(on_play_game, pattern=r"^pg:")' in src
        or "CallbackQueryHandler(on_play_game, pattern=r'^pg:')" in src
    ), "main() must register CallbackQueryHandler(on_play_game, pattern=r'^pg:')"


# -- edge: button shown on explanation regardless of post-tap state ----------


def test_on_rate_again_button_shown_when_vocab_below_min_at_send_time(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — button shown even if vocab is below MIN_VOCAB at send time
    _patch_bot_runtime(monkeypatch, conn, explanation="explanation body")
    # Only seed 1 word — the rated word — so vocab < MIN_VOCAB.
    [word_id] = _seed_chat_with_words(conn, 1)
    push_id = _seed_push(conn, word_id)
    update = _make_callback_update(f"rate:again:{push_id}:{word_id}")

    asyncio.run(bot.on_rate(update, MagicMock()))

    sent = update.callback_query.message.reply_text.call_args_list
    assert any(
        _has_play_game_button(c.kwargs.get("reply_markup")) for c in sent
    ), (
        "button must accompany the explanation regardless of vocab size; "
        "the tap-time check inside on_play_game enforces MIN_VOCAB"
    )


def test_on_rate_again_button_shown_when_game_in_progress_at_send_time(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — button shown even if a game is already in progress
    _patch_bot_runtime(monkeypatch, conn, explanation="explanation body")
    [word_id] = _seed_chat_with_words(conn, 1)
    push_id = _seed_push(conn, word_id)
    bot.games[CHAT] = MagicMock()  # game already running
    update = _make_callback_update(f"rate:again:{push_id}:{word_id}")

    asyncio.run(bot.on_rate(update, MagicMock()))

    sent = update.callback_query.message.reply_text.call_args_list
    assert any(
        _has_play_game_button(c.kwargs.get("reply_markup")) for c in sent
    ), (
        "button must accompany the explanation regardless of in-progress games; "
        "the tap-time check inside on_play_game enforces the in-progress guard"
    )


# -- edge: double-tap on play-game ------------------------------------------


def test_on_play_game_double_tap_does_not_raise(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — second tap re-runs the same checks, never raises
    _patch_bot_runtime(monkeypatch, conn)
    _seed_chat_with_words(conn, games_module.MIN_VOCAB)
    update_first = _make_callback_update("pg:start")
    update_second = _make_callback_update("pg:start")

    asyncio.run(bot.on_play_game(update_first, MagicMock()))
    # First tap should not have entered bot.games (it just posts the picker).
    # Second tap therefore re-evaluates the same gates; must not raise.
    asyncio.run(bot.on_play_game(update_second, MagicMock()))

    update_second.callback_query.answer.assert_called_once()
    update_second.callback_query.message.reply_text.assert_called_once()


# -- error: malformed pg: data ----------------------------------------------


def test_on_play_game_malformed_pg_data_does_not_raise(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # error — malformed pg: data should not raise
    _patch_bot_runtime(monkeypatch, conn)
    _seed_chat_with_words(conn, games_module.MIN_VOCAB)
    update = _make_callback_update("pg:totally-unexpected-suffix")

    # Spec: log warning, return without raising (mirrors existing handlers).
    asyncio.run(bot.on_play_game(update, MagicMock()))

    update.callback_query.answer.assert_called_once()
