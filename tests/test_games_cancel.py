"""Tests for issue #103 — `/games cancel` ends an in-flight game.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Mocking shape mirrors `tests/test_games_label_spec.py` and
`tests/test_games_picker_irregulars.py`: Telegram Update / Context are
MagicMock + AsyncMock at the architectural seam, with `bot.conn` patched
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
import games as games_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 10300


# -- helpers -----------------------------------------------------------------


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str]) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_poll = AsyncMock()
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()
    bot.irregulars.clear()
    bot.pending_game_filters.clear()


def _seed_translatable(
    conn: sqlite3.Connection, chat_id: int, words: list[str]
) -> None:
    vocab.add_words_bulk(
        conn, chat_id, words, translations=[f"tr_{w}" for w in words]
    )


def _all_replies(target: MagicMock) -> list[str]:
    out: list[str] = []
    for call in target.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _last_reply(reply_text_mock: MagicMock) -> str:
    replies = _all_replies(reply_text_mock)
    assert replies, "expected at least one reply_text call, got none"
    return replies[-1]


def _has_direction_picker(reply_markup) -> bool:
    if reply_markup is None:
        return False
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return False
    cbs = {b.callback_data for row in rows for b in row}
    return "gm:wt" in cbs and "gm:tw" in cbs


def _picker_call(update: MagicMock):
    for call in update.message.reply_text.call_args_list:
        if _has_direction_picker(call.kwargs.get("reply_markup")):
            return call
    return None


# === module surface =========================================================


def test_games_cancelled_text_constant() -> None:  # AC1, AC2 — GAMES_CANCELLED literal exists
    assert hasattr(bot, "GAMES_CANCELLED"), "bot must expose GAMES_CANCELLED"
    assert isinstance(bot.GAMES_CANCELLED, str), (
        f"GAMES_CANCELLED must be a string, got {type(bot.GAMES_CANCELLED).__name__}"
    )
    assert bot.GAMES_CANCELLED, "GAMES_CANCELLED must be a non-empty string"


def test_games_nothing_to_cancel_text_constant() -> None:  # AC3, AC4 — GAMES_NOTHING_TO_CANCEL literal exists
    assert hasattr(bot, "GAMES_NOTHING_TO_CANCEL"), (
        "bot must expose GAMES_NOTHING_TO_CANCEL"
    )
    assert isinstance(bot.GAMES_NOTHING_TO_CANCEL, str), (
        "GAMES_NOTHING_TO_CANCEL must be a string"
    )
    assert bot.GAMES_NOTHING_TO_CANCEL, (
        "GAMES_NOTHING_TO_CANCEL must be a non-empty string"
    )
    assert bot.GAMES_CANCELLED != bot.GAMES_NOTHING_TO_CANCEL, (
        "GAMES_CANCELLED and GAMES_NOTHING_TO_CANCEL must differ"
    )


# === AC1 — cancel during a vocab game =======================================


def test_cancel_during_vocab_game_clears_state_and_replies(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — chat in bot.games → cancel pops it, replies GAMES_CANCELLED
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()  # game running
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    assert CHAT not in bot.games, (
        f"AC1: bot.games must NOT contain chat after cancel; got {bot.games!r}"
    )
    assert CHAT not in bot.irregulars, (
        f"AC1: bot.irregulars must not contain chat; got {bot.irregulars!r}"
    )
    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_CANCELLED, (
        f"AC1: reply must be GAMES_CANCELLED literal; got {reply!r}"
    )


def test_cancel_during_vocab_game_clears_pending_filter(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — pending_game_filters[chat_id] is also cleared
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    assert CHAT not in bot.pending_game_filters, (
        "AC1: pending_game_filters must be cleared on cancel; "
        f"got {bot.pending_game_filters!r}"
    )


# === AC2 — cancel during an irregulars game =================================


def test_cancel_during_irregulars_game_clears_state(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — chat in bot.irregulars → cancel pops it, replies GAMES_CANCELLED
    _patch_bot(monkeypatch, conn)
    bot.irregulars[CHAT] = MagicMock()  # irregulars game running
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    assert CHAT not in bot.irregulars, (
        f"AC2: bot.irregulars must NOT contain chat after cancel; got {bot.irregulars!r}"
    )
    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_CANCELLED, (
        f"AC2: reply must be GAMES_CANCELLED literal; got {reply!r}"
    )


# === AC3 — cancel with no game ==============================================


def test_cancel_with_no_game_replies_nothing_to_cancel(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — no game, no stash → GAMES_NOTHING_TO_CANCEL
    _patch_bot(monkeypatch, conn)
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_NOTHING_TO_CANCEL, (
        f"AC3: idle cancel must reply GAMES_NOTHING_TO_CANCEL; got {reply!r}"
    )


def test_cancel_with_no_game_does_not_create_state(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — no entry written into games / irregulars / pending_game_filters
    _patch_bot(monkeypatch, conn)
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    assert CHAT not in bot.games, "AC3: cancel must not create a games entry"
    assert CHAT not in bot.irregulars, "AC3: cancel must not create an irregulars entry"
    assert CHAT not in bot.pending_game_filters, (
        "AC3: cancel must not create a pending_game_filters entry"
    )


# === AC4 — cancel with only a stashed filter ================================


def test_cancel_with_only_stashed_filter_replies_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — stashed filter alone is not "in progress" → GAMES_NOTHING_TO_CANCEL
    _patch_bot(monkeypatch, conn)
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_NOTHING_TO_CANCEL, (
        "AC4: stashed filter alone is not a game in progress; "
        f"reply must be GAMES_NOTHING_TO_CANCEL, got {reply!r}"
    )


def test_cancel_with_only_stashed_filter_clears_filter(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — pending_game_filters cleared regardless
    _patch_bot(monkeypatch, conn)
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    assert CHAT not in bot.pending_game_filters, (
        "AC4: stashed filter must be cleared on cancel even when no game is in progress; "
        f"got {bot.pending_game_filters!r}"
    )


# === AC5 — bare /games after cancel shows the picker ========================


def test_bare_games_after_cancel_shows_picker_vocab(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — after cancelling vocab game, /games shows picker (not GAMES_IN_PROGRESS)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.games[CHAT] = MagicMock()  # vocab game running

    asyncio.run(bot.cmd_games(_make_command_update(), _make_context(["cancel"])))

    update_after = _make_command_update()
    asyncio.run(bot.cmd_games(update_after, _make_context([])))

    replies = _all_replies(update_after.message.reply_text)
    assert bot.GAMES_IN_PROGRESS not in replies, (
        "AC5: bare /games after cancel must NOT reply GAMES_IN_PROGRESS; "
        f"got replies {replies!r}"
    )
    assert _picker_call(update_after) is not None, (
        "AC5: bare /games after cancel must show the gm:wt / gm:tw picker; "
        f"got calls {update_after.message.reply_text.call_args_list}"
    )


def test_bare_games_after_cancel_shows_picker_irregulars(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — after cancelling irregulars game, /games shows picker (not IRREGULARS_IN_PROGRESS)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.irregulars[CHAT] = MagicMock()  # irregulars game running

    asyncio.run(bot.cmd_games(_make_command_update(), _make_context(["cancel"])))

    update_after = _make_command_update()
    asyncio.run(bot.cmd_games(update_after, _make_context([])))

    replies = _all_replies(update_after.message.reply_text)
    assert bot.IRREGULARS_IN_PROGRESS not in replies, (
        "AC5: bare /games after cancel must NOT reply IRREGULARS_IN_PROGRESS; "
        f"got replies {replies!r}"
    )
    assert _picker_call(update_after) is not None, (
        "AC5: bare /games after cancel must show the picker; "
        f"got calls {update_after.message.reply_text.call_args_list}"
    )


# === AC6 — case-insensitive cancel ==========================================


def test_cancel_uppercase_token(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — /games CANCEL matches
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["CANCEL"])))

    assert CHAT not in bot.games, "AC6: 'CANCEL' (uppercase) must trigger cancel"
    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_CANCELLED, (
        f"AC6: uppercase cancel must reply GAMES_CANCELLED; got {reply!r}"
    )


def test_cancel_mixed_case_token(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — /games Cancel matches
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["Cancel"])))

    assert CHAT not in bot.games, "AC6: 'Cancel' (mixed-case) must trigger cancel"
    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_CANCELLED, (
        f"AC6: mixed-case cancel must reply GAMES_CANCELLED; got {reply!r}"
    )


def test_cancel_token_with_whitespace(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — /games "  cancel  " (extra whitespace) matches via .strip()
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["  cancel  "])))

    assert CHAT not in bot.games, (
        "AC6: whitespace-padded 'cancel' must trigger cancel after .strip()"
    )
    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_CANCELLED, (
        f"AC6: whitespace-padded cancel must reply GAMES_CANCELLED; got {reply!r}"
    )


# === AC7 — multi-token args fall through ====================================


def test_cancel_extra_arg_falls_through_to_label_spec(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — args=["cancel","extra"] is NOT cancel; goes through parse_label_spec
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.games[CHAT] = MagicMock()  # game still running

    parser_calls: list[tuple] = []
    real_parser = vocab.parse_label_spec

    def _spy_parser(*args, **kwargs):
        parser_calls.append((args, kwargs))
        return real_parser(*args, **kwargs)

    monkeypatch.setattr(vocab, "parse_label_spec", _spy_parser)

    update = _make_command_update()
    asyncio.run(bot.cmd_games(update, _make_context(["cancel", "extra"])))

    # AC7: cancel branch must NOT have fired — game is still in progress.
    assert CHAT in bot.games, (
        "AC7: ['cancel','extra'] must NOT trigger cancel; game must still be running"
    )
    replies = _all_replies(update.message.reply_text)
    assert bot.GAMES_CANCELLED not in replies, (
        f"AC7: multi-token args must not reply GAMES_CANCELLED; got {replies!r}"
    )


# === AC8 — cancel runs before the in-progress short-circuit =================


def test_cancel_runs_before_in_progress_short_circuit(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC8 — cancel branch precedes GAMES_IN_PROGRESS short-circuit
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()  # vocab game in flight
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    replies = _all_replies(update.message.reply_text)
    assert bot.GAMES_IN_PROGRESS not in replies, (
        "AC8: cancel must run BEFORE the GAMES_IN_PROGRESS short-circuit; "
        f"otherwise cancel could never run during a game. Got replies {replies!r}"
    )
    assert bot.GAMES_CANCELLED in replies, (
        f"AC8: cancel must succeed even when a game is in progress; got {replies!r}"
    )
    assert CHAT not in bot.games, "AC8: game must actually be cancelled, not blocked"


def test_cancel_runs_before_irregulars_in_progress(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC8 — cancel branch precedes IRREGULARS_IN_PROGRESS short-circuit
    _patch_bot(monkeypatch, conn)
    bot.irregulars[CHAT] = MagicMock()  # irregulars game in flight
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))

    replies = _all_replies(update.message.reply_text)
    assert bot.IRREGULARS_IN_PROGRESS not in replies, (
        "AC8: cancel must run BEFORE the IRREGULARS_IN_PROGRESS short-circuit; "
        f"got replies {replies!r}"
    )
    assert bot.GAMES_CANCELLED in replies, (
        f"AC8: cancel must succeed during an irregulars game; got {replies!r}"
    )
    assert CHAT not in bot.irregulars, (
        "AC8: irregulars game must actually be cancelled, not blocked"
    )


# === error / idempotence ====================================================


def test_cancel_idempotent_double_call(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # error: double-cancel just replies GAMES_NOTHING_TO_CANCEL
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()

    # First cancel succeeds.
    asyncio.run(bot.cmd_games(_make_command_update(), _make_context(["cancel"])))
    assert CHAT not in bot.games

    # Second cancel must not raise; must reply GAMES_NOTHING_TO_CANCEL.
    update2 = _make_command_update()
    asyncio.run(bot.cmd_games(update2, _make_context(["cancel"])))

    reply = _last_reply(update2.message.reply_text)
    assert reply == bot.GAMES_NOTHING_TO_CANCEL, (
        "double-cancel must reply GAMES_NOTHING_TO_CANCEL (idempotent); "
        f"got {reply!r}"
    )
