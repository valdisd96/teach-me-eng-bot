"""Tests for issue #85 — `/games <spec>` builds the quiz pool from a label-filtered subset.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. Behaviour
asserted here is derived from that spec, not from `cmd_games` / `on_games_menu`
bodies.

Mocking shape mirrors `tests/test_list_label_spec.py` and
`tests/test_play_game_button.py` — Telegram Update / Context are MagicMock +
AsyncMock at the architectural seam, with `bot.conn` patched to the temp-DB
fixture.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import games as games_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 8585


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
    bot.pending_game_filters.clear()


def _word_id(conn: sqlite3.Connection, chat_id: int, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat_id, text)
    ).fetchone()["id"]


def _attach(conn: sqlite3.Connection, chat_id: int, word: str, label: str) -> None:
    wid = _word_id(conn, chat_id, word)
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, chat_id, label))


def _seed_translatable(
    conn: sqlite3.Connection, chat_id: int, words: list[str]
) -> None:
    """Insert `words` into `chat_id` with non-empty translations."""
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


def _picker_buttons(reply_markup) -> list:
    if reply_markup is None:
        return []
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return []
    return [btn for row in rows for btn in row]


def _has_direction_picker(reply_markup) -> bool:
    btns = _picker_buttons(reply_markup)
    cbs = {b.callback_data for b in btns}
    return "gm:wt" in cbs and "gm:tw" in cbs


def _picker_call(update: MagicMock):
    """Return the reply_text call that posts the gm:wt / gm:tw picker, if any."""
    for call in update.message.reply_text.call_args_list:
        if _has_direction_picker(call.kwargs.get("reply_markup")):
            return call
    return None


# === module surface ==========================================================


def test_games_no_label_match_constant_text() -> None:  # AC3 — exact wording per spec
    assert bot.GAMES_NO_LABEL_MATCH == (
        "no words match those labels — try fewer filters or `/label` more words"
    ), f"GAMES_NO_LABEL_MATCH text drift: {bot.GAMES_NO_LABEL_MATCH!r}"


def test_pending_game_filters_module_dict_exists() -> None:  # AC1, AC4 — module-level stash
    assert hasattr(bot, "pending_game_filters"), (
        "bot must expose a module-level `pending_game_filters` dict"
    )
    assert isinstance(bot.pending_game_filters, dict), (
        f"bot.pending_game_filters must be a dict, got {type(bot.pending_game_filters).__name__}"
    )


# === _playable_rows ==========================================================


def test_playable_rows_default_no_names_returns_all_translatable(
    conn: sqlite3.Connection,
) -> None:  # AC2 — names=None → all translatable rows (today's behaviour)
    _seed_translatable(conn, CHAT, ["alpha", "beta", "gamma", "delta"])

    rows = bot._playable_rows(conn, CHAT)

    texts = sorted(r["text"] for r in rows)
    assert texts == ["alpha", "beta", "delta", "gamma"], (
        f"expected all 4 translatable rows, got {texts}"
    )


def test_playable_rows_with_names_filters_by_AND(
    conn: sqlite3.Connection,
) -> None:  # AC1 — names list → AND across labels
    _seed_translatable(conn, CHAT, ["horse", "pill", "aspirin"])
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "horse", "type:animal")
    _attach(conn, CHAT, "pill", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")
    _attach(conn, CHAT, "aspirin", "pos:noun")
    _attach(conn, CHAT, "aspirin", "type:medicine")

    rows = bot._playable_rows(conn, CHAT, ["pos:noun", "type:medicine"])

    texts = sorted(r["text"] for r in rows)
    assert texts == ["aspirin", "pill"], (
        f"AC1: AND across pos:noun + type:medicine should keep only aspirin + pill; got {texts}"
    )


def test_playable_rows_empty_names_falls_to_unfiltered(
    conn: sqlite3.Connection,
) -> None:  # edge — names=[] (falsy) treated as unfiltered, not "match nothing"
    _seed_translatable(conn, CHAT, ["alpha", "beta"])

    rows = bot._playable_rows(conn, CHAT, [])

    texts = sorted(r["text"] for r in rows)
    assert texts == ["alpha", "beta"], (
        f"empty names list must take the unfiltered branch (== bare /games); got {texts}"
    )


def test_playable_rows_filters_blank_translation_after_label_match(
    conn: sqlite3.Connection,
) -> None:  # boundary — even with a label match, blank translations are excluded
    # Seeded translatable rows are matched by the label; an extra row with
    # empty translation but the same label must NOT make it past the filter.
    _seed_translatable(conn, CHAT, ["horse", "pill"])
    vocab.add_word(conn, CHAT, "ghost")  # no translation
    for w in ("horse", "pill", "ghost"):
        _attach(conn, CHAT, w, "pos:noun")

    rows = bot._playable_rows(conn, CHAT, ["pos:noun"])

    texts = sorted(r["text"] for r in rows)
    assert texts == ["horse", "pill"], (
        f"row with NULL/empty translation must still be filtered out; got {texts}"
    )
    assert "ghost" not in texts


# === cmd_games — bare /games (AC2) ==========================================


def test_cmd_games_no_args_shows_picker(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — bare /games posts the gm:wt / gm:tw picker, as today
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    call = _picker_call(update)
    assert call is not None, (
        "bare /games must post the direction picker; "
        f"got calls {update.message.reply_text.call_args_list}"
    )


def test_cmd_games_no_args_does_not_set_pending_filter(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — bare /games leaves no stash entry
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    assert CHAT not in bot.pending_game_filters, (
        "bare /games must not leave a stashed filter; "
        f"got {bot.pending_game_filters!r}"
    )


def test_cmd_games_no_args_overwrites_existing_stash(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — /games <spec> then bare /games clears the stash to "no filter"
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])  # leftover from a prior /games <spec>
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([])))

    assert CHAT not in bot.pending_game_filters, (
        "second /games (no args) must overwrite/clear the prior stash so the "
        "subsequent picker tap starts unfiltered; "
        f"got {bot.pending_game_filters!r}"
    )


# === cmd_games — filtered branch (AC1, AC3, AC4) ============================


def test_cmd_games_filtered_with_match_shows_picker_and_stashes(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1, AC4 — viable filtered pool ⇒ picker shown + filter stashed
    _patch_bot(monkeypatch, conn)
    # Seed >= MIN_VOCAB matching rows so the filtered pool is viable.
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching + ["other"])
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    _attach(conn, CHAT, "other", "pos:verb")
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["pos:noun"])))

    call = _picker_call(update)
    assert call is not None, (
        "viable filtered pool must show the gm:wt / gm:tw picker; "
        f"got {update.message.reply_text.call_args_list}"
    )
    assert bot.pending_game_filters.get(CHAT) == ("all", ["pos:noun"]), (
        f"filter must be stashed for the picker tap; got {bot.pending_game_filters!r}"
    )


def test_cmd_games_filtered_zero_matches_replies_no_label_match(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3, edge — 0 matches ⇒ GAMES_NO_LABEL_MATCH, no picker
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    # No labels attached — pos:noun yields zero rows.
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["pos:noun"])))

    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_NO_LABEL_MATCH, (
        f"AC3 mandates GAMES_NO_LABEL_MATCH literal on zero-match filtered pool; got {reply!r}"
    )
    assert _picker_call(update) is None, (
        "AC3: no picker may be shown when filter pool is below MIN_VOCAB"
    )


def test_cmd_games_filtered_below_min_replies_no_label_match(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3, edge — 1–3 matches ⇒ GAMES_NO_LABEL_MATCH, no picker
    _patch_bot(monkeypatch, conn)
    n_below = games_module.MIN_VOCAB - 1
    assert n_below >= 1, "MIN_VOCAB must be >= 2 for this edge case to be meaningful"
    matching = [f"m{i}" for i in range(n_below)]
    extra = [f"x{i}" for i in range(games_module.MIN_VOCAB)]  # plenty of unlabelled rows
    _seed_translatable(conn, CHAT, matching + extra)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["pos:noun"])))

    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_NO_LABEL_MATCH, (
        "AC3: filtered pool < MIN_VOCAB ⇒ GAMES_NO_LABEL_MATCH (not GAMES_NEED_VOCAB); "
        f"got {reply!r}"
    )
    assert _picker_call(update) is None


def test_cmd_games_filtered_zero_matches_does_not_stash(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — refusal path leaves no stashed filter behind
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context(["pos:noun"])))

    assert CHAT not in bot.pending_game_filters, (
        "GAMES_NO_LABEL_MATCH path must not leave a stashed filter; "
        f"got {bot.pending_game_filters!r}"
    )


# === cmd_games — malformed spec (AC5) =======================================


def test_cmd_games_malformed_spec_warning_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5, error — malformed spec ⇒ "⚠️ <message>"
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([":foo"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f'AC5: malformed spec must yield "⚠️ "-prefixed reply; got {reply!r}'
    )
    assert "malformed label spec" in reply, (
        f'reply must echo the parser\'s "malformed label spec" wording; got {reply!r}'
    )
    assert ":foo" in reply, f"reply must include the offending token; got {reply!r}"


def test_cmd_games_malformed_spec_no_words_query(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — malformed spec performs no DB read of words
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )

    def _boom(*args, **kwargs):
        raise AssertionError("words must not be queried on a malformed spec")

    monkeypatch.setattr(vocab, "list_words", _boom)
    monkeypatch.setattr(vocab, "words_matching_labels", _boom)

    update = _make_command_update()
    # Must not raise — handler must short-circuit before either word-read call.
    asyncio.run(bot.cmd_games(update, _make_context(["a:b:c"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC5: ⚠️-prefixed reply expected on malformed spec; got {reply!r}"
    )


def test_cmd_games_malformed_spec_no_picker_no_stash(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — malformed spec ⇒ no picker, no state stashed
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    update = _make_command_update()

    asyncio.run(bot.cmd_games(update, _make_context([":foo"])))

    assert _picker_call(update) is None, (
        "AC5: malformed spec must not surface the gm:wt / gm:tw picker"
    )
    assert CHAT not in bot.pending_game_filters, (
        f"AC5: malformed spec must not stash any filter; got {bot.pending_game_filters!r}"
    )


# === cmd_games — in-progress short-circuit (AC6) ============================


def test_cmd_games_in_progress_with_spec_args_short_circuits(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6, edge — in-progress wins; no parsing, no stash mutation
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    bot.games[CHAT] = MagicMock()  # game already running
    bot.pending_game_filters[CHAT] = ("all", ["was-here"])  # leftover; must remain untouched

    # parse_label_spec must not even be called — guard with a boom spy.
    def _parser_boom(*args, **kwargs):
        raise AssertionError("parse_label_spec must not run when a game is in progress")

    monkeypatch.setattr(vocab, "parse_label_spec", _parser_boom)

    update = _make_command_update()
    asyncio.run(bot.cmd_games(update, _make_context([":malformed"])))

    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_IN_PROGRESS, (
        f"AC6: in-progress reply must be GAMES_IN_PROGRESS literal; got {reply!r}"
    )
    assert _picker_call(update) is None, (
        "AC6: in-progress short-circuit must not show the picker"
    )
    assert bot.pending_game_filters.get(CHAT) == ("all", ["was-here"]), (
        "AC6: in-progress branch must not mutate the stash; "
        f"got {bot.pending_game_filters!r}"
    )


# === cmd_games — parser dedup / case (edge) =================================


def test_cmd_games_dedups_mixed_case_spec_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — duplicate / mixed-case tokens normalise via parse_label_spec
    _patch_bot(monkeypatch, conn)
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    update = _make_command_update()

    asyncio.run(
        bot.cmd_games(update, _make_context(["  Pos:Noun ", "pos:noun"]))
    )

    assert _picker_call(update) is not None, (
        "duplicates + casing must normalise to a single 'pos:noun' and still "
        "show the picker for the viable filtered pool"
    )
    stashed = bot.pending_game_filters.get(CHAT)
    assert stashed == ("all", ["pos:noun"]), (
        f"stash must hold the deduped/lowercased label list; got {stashed!r}"
    )


# === on_games_menu — filter survives the picker tap (AC1, AC4) ==============


def test_on_games_menu_with_stashed_filter_uses_filtered_pool(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1, AC4 — picker tap consumes stash; rounds drawn only from filtered pool
    _patch_bot(monkeypatch, conn)
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    other = [f"o{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching + other)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])

    matching_ids = {_word_id(conn, CHAT, w) for w in matching}

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    game = bot.games.get(CHAT)
    assert game is not None, "AC4: a viable filtered pool must start a game"
    round_ids = {r.word_id for r in game.rounds}
    assert round_ids, "AC4: game must have at least one round"
    assert round_ids.issubset(matching_ids), (
        "AC4: rounds must be drawn ONLY from the filtered pool; "
        f"unexpected ids = {round_ids - matching_ids}"
    )


def test_on_games_menu_no_stash_uses_full_pool(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — picker tap with no stash uses the full translatable pool
    _patch_bot(monkeypatch, conn)
    everyone = [f"w{i}" for i in range(games_module.MIN_VOCAB * 2)]
    _seed_translatable(conn, CHAT, everyone)
    # Half labelled, half not — but no stash is set, so labels must NOT filter.
    for w in everyone[: games_module.MIN_VOCAB]:
        _attach(conn, CHAT, w, "pos:noun")
    every_id = {_word_id(conn, CHAT, w) for w in everyone}

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    game = bot.games.get(CHAT)
    assert game is not None, "AC2: no-stash + sufficient vocab must start a game"
    round_ids = {r.word_id for r in game.rounds}
    assert round_ids.issubset(every_id), "rounds must come from the chat's vocab"
    # The pool draw is randomised, so we cannot assert specific IDs; instead
    # assert that the **possible** pool wasn't narrowed: pick a seed-independent
    # invariant — the unlabelled half is reachable.
    # We verify reachability indirectly by checking that the candidate pool
    # used by the round drawer matches the unfiltered set: the chosen IDs must
    # all live inside `every_id` (already asserted) AND must not be confined
    # to the labelled half across many calls.
    seen_unlabelled = False
    unlabelled_ids = {
        _word_id(conn, CHAT, w) for w in everyone[games_module.MIN_VOCAB :]
    }
    for _ in range(20):
        bot.games.pop(CHAT, None)
        upd = _make_callback_update("gm:wt")
        asyncio.run(bot.on_games_menu(upd, _make_context([])))
        g = bot.games[CHAT]
        if {r.word_id for r in g.rounds} & unlabelled_ids:
            seen_unlabelled = True
            break
    assert seen_unlabelled, (
        "AC2: no-stash branch must draw from the FULL pool — unlabelled words "
        "should be reachable, not silently filtered"
    )


def test_on_games_menu_pops_stash_on_successful_start(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — successful round-start consumes (pops) the stash entry
    _patch_bot(monkeypatch, conn)
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    assert CHAT not in bot.pending_game_filters, (
        "AC4: the stash entry must be popped after a successful start; "
        f"got {bot.pending_game_filters!r}"
    )


def test_on_games_menu_filtered_below_min_replies_no_label_match(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — stash present but pool < MIN_VOCAB ⇒ GAMES_NO_LABEL_MATCH (not NEED_VOCAB)
    _patch_bot(monkeypatch, conn)
    # Plenty of overall vocab so the unfiltered branch would NOT trip GAMES_NEED_VOCAB.
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB * 2)]
    )
    # Only 1 row labelled — filtered pool < MIN_VOCAB.
    _attach(conn, CHAT, "w0", "pos:noun")
    bot.pending_game_filters[CHAT] = ("all", ["pos:noun"])

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    reply = _last_reply(update.callback_query.message.reply_text)
    assert reply == bot.GAMES_NO_LABEL_MATCH, (
        "AC3: filter present + pool < MIN_VOCAB ⇒ GAMES_NO_LABEL_MATCH (not GAMES_NEED_VOCAB); "
        f"got {reply!r}"
    )
    assert CHAT not in bot.games, "AC3: no game should be started on this branch"


def test_on_games_menu_no_stash_below_min_replies_need_vocab(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — no stash + pool < MIN_VOCAB ⇒ GAMES_NEED_VOCAB (today's behaviour)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB - 1)]
    )

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    reply = _last_reply(update.callback_query.message.reply_text)
    assert reply == bot.GAMES_NEED_VOCAB, (
        "AC2: no stash + pool < MIN_VOCAB must keep today's GAMES_NEED_VOCAB reply; "
        f"got {reply!r}"
    )


def test_on_games_menu_stale_tap_uses_current_stash(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — stale gm: tap uses the current stash (latest /games wins)
    _patch_bot(monkeypatch, conn)
    a_words = [f"a{i}" for i in range(games_module.MIN_VOCAB)]
    b_words = [f"b{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, a_words + b_words)
    for w in a_words:
        _attach(conn, CHAT, w, "spec:a")
    for w in b_words:
        _attach(conn, CHAT, w, "spec:b")
    # Latest /games wins → stash carries spec:b at tap time.
    bot.pending_game_filters[CHAT] = ("all", ["spec:b"])
    b_ids = {_word_id(conn, CHAT, w) for w in b_words}
    a_ids = {_word_id(conn, CHAT, w) for w in a_words}

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    game = bot.games[CHAT]
    round_ids = {r.word_id for r in game.rounds}
    assert round_ids.issubset(b_ids), (
        "stale-tap: rounds must be drawn from the CURRENT stash (spec:b), not from "
        f"a prior spec; round_ids={round_ids}, expected ⊆ {b_ids}, a_ids={a_ids}"
    )
    assert not (round_ids & a_ids), (
        "stale-tap: no spec:a words may leak into a spec:b game"
    )


# === COMMANDS surface =======================================================


def test_commands_games_description_mentions_label_filter() -> None:  # AC1 — help text mentions label filter
    games_desc = next((d for n, d in bot.COMMANDS if n == "games"), None)
    assert games_desc is not None, "COMMANDS must contain a 'games' entry"
    desc_l = games_desc.lower()
    assert "label" in desc_l or "filter" in desc_l, (
        "AC1: /games COMMANDS entry should advertise the label-spec filter; "
        f"got {games_desc!r}"
    )
