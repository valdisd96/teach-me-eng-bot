"""Tests for issue #102 — fold irregular verbs into the /games picker.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Mocking shape mirrors `tests/test_games_label_spec.py` and
`tests/test_play_game_button.py`: Telegram Update / Context are MagicMock +
AsyncMock at the architectural seam, with `bot.conn` patched to the temp-DB
fixture.
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
import irregular_verbs as irregular_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 10210


# -- helpers -----------------------------------------------------------------


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(callback_data: str, chat_id: int = CHAT) -> MagicMock:
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


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args if args is not None else []
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
    if not words:
        vocab.ensure_chat(conn, chat_id)
        return
    vocab.add_words_bulk(
        conn, chat_id, words, translations=[f"tr_{w}" for w in words]
    )


def _word_id(conn: sqlite3.Connection, chat_id: int, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat_id, text)
    ).fetchone()["id"]


def _attach(conn: sqlite3.Connection, chat_id: int, word: str, label: str) -> None:
    wid = _word_id(conn, chat_id, word)
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, chat_id, label))


def _all_replies(target: MagicMock) -> list[str]:
    out: list[str] = []
    for call in target.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _picker_buttons(reply_markup) -> list:
    if reply_markup is None:
        return []
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return []
    return [btn for row in rows for btn in row]


def _picker_call_from(target: MagicMock):
    """Return the reply_text call whose keyboard carries any gm:* button."""
    for call in target.call_args_list:
        btns = _picker_buttons(call.kwargs.get("reply_markup"))
        if any(getattr(b, "callback_data", "").startswith("gm:") for b in btns):
            return call
    return None


# === AC1 — bare /games with ≥ MIN_VOCAB shows three-button picker ============


def test_cmd_games_no_args_full_picker_has_three_buttons(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — bare /games with ≥MIN_VOCAB shows gm:wt + gm:tw + gm:irr
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    call = _picker_call_from(update.message.reply_text)
    assert call is not None, (
        "AC1: bare /games (≥MIN_VOCAB) must post a picker; "
        f"got calls {update.message.reply_text.call_args_list}"
    )
    btns = _picker_buttons(call.kwargs.get("reply_markup"))
    by_data = {b.callback_data: b.text for b in btns}
    assert by_data.get("gm:wt") == "Word → Translation", (
        f"AC1: missing/mislabeled gm:wt; got {by_data}"
    )
    assert by_data.get("gm:tw") == "Translation → Word", (
        f"AC1: missing/mislabeled gm:tw; got {by_data}"
    )
    assert by_data.get("gm:irr") == "Irregular verbs", (
        f"AC1: missing/mislabeled gm:irr; got {by_data}"
    )


def test_cmd_games_no_args_clears_pending_filter(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — bare /games clears any prior pending_game_filters[chat]
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.pending_game_filters[CHAT] = ["pos:noun"]  # leftover from prior /games <spec>
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    assert CHAT not in bot.pending_game_filters, (
        "AC1: bare /games must clear pending_game_filters[chat]; "
        f"got {bot.pending_game_filters!r}"
    )


# === AC2 — bare /games with < MIN_VOCAB still shows the irregulars button ====


def test_cmd_games_no_args_below_min_vocab_picker_has_only_irr(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — < MIN_VOCAB ⇒ picker shows ONLY gm:irr (no gm:wt/gm:tw)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(conn, CHAT, [])  # zero translatable rows
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    call = _picker_call_from(update.message.reply_text)
    assert call is not None, (
        "AC2: even with 0 vocab, /games must post a picker carrying gm:irr; "
        f"got {update.message.reply_text.call_args_list}"
    )
    cbs = {b.callback_data for b in _picker_buttons(call.kwargs.get("reply_markup"))}
    assert "gm:irr" in cbs, f"AC2: picker must include gm:irr; got {cbs}"
    assert "gm:wt" not in cbs, (
        f"AC2: gm:wt must NOT appear when vocab < MIN_VOCAB; got {cbs}"
    )
    assert "gm:tw" not in cbs, (
        f"AC2: gm:tw must NOT appear when vocab < MIN_VOCAB; got {cbs}"
    )


def test_cmd_games_no_args_below_min_vocab_no_need_vocab_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — < MIN_VOCAB ⇒ GAMES_NEED_VOCAB is NOT sent (today's refusal removed)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(conn, CHAT, [])
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    replies = _all_replies(update.message.reply_text)
    assert bot.GAMES_NEED_VOCAB not in replies, (
        "AC2: bare /games (no vocab) must NOT emit GAMES_NEED_VOCAB any more; "
        f"got {replies!r}"
    )


# === AC3 — /games <spec> picker is vocab-only (no gm:irr) ====================


def test_cmd_games_with_spec_picker_excludes_irr(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — viable filtered pool ⇒ exactly gm:wt + gm:tw; gm:irr absent
    _patch_bot(monkeypatch, conn)
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["pos:noun"])))

    call = _picker_call_from(update.message.reply_text)
    assert call is not None, (
        "AC3: viable filtered pool must show the gm:wt / gm:tw picker; "
        f"got {update.message.reply_text.call_args_list}"
    )
    cbs = {b.callback_data for b in _picker_buttons(call.kwargs.get("reply_markup"))}
    assert cbs == {"gm:wt", "gm:tw"}, (
        f"AC3: spec picker must contain exactly gm:wt + gm:tw; got {cbs}"
    )
    assert "gm:irr" not in cbs, (
        f"AC3: gm:irr must NOT appear in the /games <spec> picker; got {cbs}"
    )
    # Today's stash behaviour preserved (sanity: AC3 says "stashed").
    assert bot.pending_game_filters.get(CHAT) == ["pos:noun"], (
        f"AC3: filter must be stashed; got {bot.pending_game_filters!r}"
    )


# === AC4 — /games while a vocab game is in progress ==========================


def test_cmd_games_in_vocab_game_short_circuits(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — chat in bot.games ⇒ GAMES_IN_PROGRESS literal, no picker
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.games[CHAT] = MagicMock()  # in-flight vocab game
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    replies = _all_replies(update.message.reply_text)
    assert bot.GAMES_IN_PROGRESS in replies, (
        f"AC4: must reply GAMES_IN_PROGRESS; got {replies!r}"
    )
    assert _picker_call_from(update.message.reply_text) is None, (
        "AC4: in-progress short-circuit must not post a picker"
    )


# === AC5 — /games while an irregular-verbs game is in progress ===============


def test_cmd_games_in_irregular_game_short_circuits(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — chat in bot.irregulars ⇒ IRREGULARS_IN_PROGRESS, no picker
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.irregulars[CHAT] = MagicMock()  # in-flight irregular-verbs game
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    replies = _all_replies(update.message.reply_text)
    assert bot.IRREGULARS_IN_PROGRESS in replies, (
        f"AC5: must reply IRREGULARS_IN_PROGRESS; got {replies!r}"
    )
    assert _picker_call_from(update.message.reply_text) is None, (
        "AC5: irregular-in-progress short-circuit must not post a picker"
    )


# === AC6 — gm:irr tap (no game in flight) starts an irregular-verbs game =====


def test_on_games_menu_irr_starts_irregular_game(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — bot.irregulars[chat] populated; reply text == _format_irregular_prompt(game)
    _patch_bot(monkeypatch, conn)
    update = _make_callback_update("gm:irr")

    asyncio.run(bot.on_games_menu(update, _make_context([])))

    game = bot.irregulars.get(CHAT)
    assert game is not None, (
        "AC6: gm:irr tap (no game in flight) must populate bot.irregulars[chat]"
    )
    assert isinstance(game, irregular_module.Game), (
        f"AC6: bot.irregulars[chat] must be an irregular_verbs.Game; got {type(game).__name__}"
    )
    assert game.chat_id == CHAT, (
        f"AC6: Game.chat_id must match the tapping chat; got {game.chat_id}"
    )
    assert game.rounds, "AC6: started game must have at least one round"

    sent = _all_replies(update.callback_query.message.reply_text)
    expected = bot._format_irregular_prompt(game)
    assert expected in sent, (
        "AC6: reply text must equal _format_irregular_prompt(game) — same first "
        f"message as today's /irregulars; got {sent!r}, expected {expected!r}"
    )
    # No vocab game leaked.
    assert CHAT not in bot.games, (
        "AC6: gm:irr must NOT start a vocab game; got bot.games[CHAT] populated"
    )


# === AC7 — gm:irr tap while irregular game already in flight =================


def test_on_games_menu_irr_in_irregular_game_short_circuits(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — chat in bot.irregulars ⇒ IRREGULARS_IN_PROGRESS, no new game
    _patch_bot(monkeypatch, conn)
    sentinel = MagicMock()
    bot.irregulars[CHAT] = sentinel  # in-flight irregular game
    update = _make_callback_update("gm:irr")

    asyncio.run(bot.on_games_menu(update, _make_context([])))

    replies = _all_replies(update.callback_query.message.reply_text)
    assert bot.IRREGULARS_IN_PROGRESS in replies, (
        f"AC7: must reply IRREGULARS_IN_PROGRESS; got {replies!r}"
    )
    assert bot.irregulars[CHAT] is sentinel, (
        "AC7: existing irregular game must not be replaced; "
        f"got {bot.irregulars[CHAT]!r}"
    )


# === AC8 — gm:irr tap while a vocab game is in flight ========================


def test_on_games_menu_irr_in_vocab_game_short_circuits(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC8 — chat in bot.games ⇒ GAMES_IN_PROGRESS, no irregular game started
    _patch_bot(monkeypatch, conn)
    bot.games[CHAT] = MagicMock()  # in-flight vocab game
    update = _make_callback_update("gm:irr")

    asyncio.run(bot.on_games_menu(update, _make_context([])))

    replies = _all_replies(update.callback_query.message.reply_text)
    assert bot.GAMES_IN_PROGRESS in replies, (
        f"AC8: must reply GAMES_IN_PROGRESS; got {replies!r}"
    )
    assert CHAT not in bot.irregulars, (
        "AC8: gm:irr while vocab game in progress must NOT start an irregular game; "
        f"got bot.irregulars[CHAT] populated"
    )


# === AC9 — /irregulars no longer in COMMANDS =================================


def test_bot_commands_no_irregulars_entry() -> None:  # AC9 — COMMANDS has no 'irregulars'
    names = {name for name, _ in bot.COMMANDS}
    assert "irregulars" not in names, (
        f"AC9: COMMANDS must not contain 'irregulars'; got {sorted(names)}"
    )


# === AC10 — cmd_irregulars removed ===========================================


def test_bot_module_lacks_cmd_irregulars() -> None:  # AC10 — no bot.cmd_irregulars
    assert not hasattr(bot, "cmd_irregulars"), (
        "AC10: bot.cmd_irregulars must be removed (replaced by gm:irr branch)"
    )


# === AC11 — /irregulars CommandHandler not registered in main() ==============


def test_main_does_not_register_irregulars_handler() -> None:  # AC11 — source-level check
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert 'CommandHandler("irregulars"' not in src, (
        'AC11: main() must not register CommandHandler("irregulars", ...)'
    )
    assert "CommandHandler('irregulars'" not in src, (
        "AC11: main() must not register CommandHandler('irregulars', ...)"
    )


# === AC12 — on_play_game post-forgot picker stays 2-button ===================


def test_on_play_game_picker_excludes_irr(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC12 — post-forgot 🎮 button keeps gm:wt + gm:tw only (no gm:irr)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_callback_update("pg:start")

    asyncio.run(bot.on_play_game(update, _make_context([])))

    update.callback_query.message.reply_text.assert_called_once()
    call = update.callback_query.message.reply_text.call_args
    btns = _picker_buttons(call.kwargs.get("reply_markup"))
    cbs = {b.callback_data for b in btns}
    assert cbs == {"gm:wt", "gm:tw"}, (
        f"AC12: on_play_game picker must remain exactly gm:wt + gm:tw; got {cbs}"
    )
    assert "gm:irr" not in cbs, (
        f"AC12: gm:irr must NOT be added to the post-forgot picker; got {cbs}"
    )
