"""Tests for issue #86 — `/focus` persistent label-spec filter.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Behaviour asserted here is derived from that spec; nothing is inferred from
function bodies.

Public surface under test:
- `vocab.get_focus_spec` / `vocab.set_focus_spec` (new).
- `vocab.select_word(..., names=...)` (extended).
- `scheduler.compose_push(..., names=...)` (extended).
- `bot.cmd_focus` (new) + COMMANDS entry + handler registration.
- `bot.dispatch_push` reading the focus + passing it through.
- `bot.on_play_game` seeding/clearing `pending_game_filters` from the focus.

Mocking shape mirrors `tests/test_play_game_button.py` and
`tests/test_games_label_spec.py` — Telegram Update / Context are MagicMock +
AsyncMock at the architectural seam, with `bot.conn` patched to the temp-DB
fixture.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import db as db_mod  # noqa: E402
import games as games_module  # noqa: E402
import scheduler as sched_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 8686


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
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()
    bot.pending_game_filters.clear()


def _all_replies(reply_mock: MagicMock) -> list[str]:
    out: list[str] = []
    for call in reply_mock.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _last_reply(reply_mock: MagicMock) -> str:
    replies = _all_replies(reply_mock)
    assert replies, "expected at least one reply_text call, got none"
    return replies[-1]


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
    vocab.add_words_bulk(
        conn, chat_id, words, translations=[f"tr_{w}" for w in words]
    )


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


# === vocab.set_focus_spec / vocab.get_focus_spec =============================


def test_set_focus_spec_stores_value(
    conn: sqlite3.Connection,
) -> None:  # AC1 — set stores the normalized spec text verbatim
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:medicine")

    assert vocab.get_focus_spec(conn, CHAT) == "pos:noun type:medicine", (
        f"stored focus_spec must round-trip exactly; got "
        f"{vocab.get_focus_spec(conn, CHAT)!r}"
    )


def test_set_focus_spec_idempotent_single_row(
    conn: sqlite3.Connection,
) -> None:  # AC1 — re-call leaves a single chat row with the same value
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:medicine")
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:medicine")

    rows = conn.execute(
        "SELECT chat_id, focus_spec FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchall()
    assert len(rows) == 1, (
        f"second set_focus_spec must NOT create a duplicate chat row; got {len(rows)}"
    )
    assert rows[0]["focus_spec"] == "pos:noun type:medicine", (
        f"value must be stable across idempotent set; got {rows[0]['focus_spec']!r}"
    )


def test_set_focus_spec_creates_chat_row_if_missing(
    conn: sqlite3.Connection,
) -> None:  # edge — /focus before /start (chat row absent)
    pre = conn.execute(
        "SELECT 1 FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    assert pre is None, "precondition: chat row must NOT exist before set_focus_spec"

    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    row = conn.execute(
        "SELECT focus_spec FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    assert row is not None, "set_focus_spec must auto-create the chat row"
    assert row["focus_spec"] == "pos:noun", (
        f"focus_spec column must hold the supplied value; got {row['focus_spec']!r}"
    )


def test_set_focus_spec_none_clears_to_null(
    conn: sqlite3.Connection,
) -> None:  # AC2 + example — None clears the column to NULL
    vocab.set_focus_spec(conn, CHAT, "pos:noun")
    assert vocab.get_focus_spec(conn, CHAT) == "pos:noun"  # precondition

    vocab.set_focus_spec(conn, CHAT, None)

    assert vocab.get_focus_spec(conn, CHAT) is None, (
        f"set_focus_spec(None) must NULL the column; got "
        f"{vocab.get_focus_spec(conn, CHAT)!r}"
    )


def test_get_focus_spec_returns_none_when_chat_missing(
    conn: sqlite3.Connection,
) -> None:  # edge — chat row absent: returns None, never raises
    pre = conn.execute(
        "SELECT 1 FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    assert pre is None, "precondition: chat row must not exist"

    assert vocab.get_focus_spec(conn, CHAT) is None, (
        "get_focus_spec on missing chat row must return None, not raise"
    )


def test_get_focus_spec_returns_none_when_unset(
    conn: sqlite3.Connection,
) -> None:  # AC2 — chat exists, focus_spec NULL → None
    vocab.ensure_chat(conn, CHAT)

    assert vocab.get_focus_spec(conn, CHAT) is None, (
        "freshly-created chat row must have focus_spec NULL"
    )


def test_focus_spec_persists_across_new_connection(
    tmp_path: Path,
) -> None:  # AC6 — fresh connection on the same DB sees the stored value
    db_path = tmp_path / "vocab.db"
    c1 = db_mod.connect(db_path)
    db_mod.init_db(c1)
    vocab.set_focus_spec(c1, CHAT, "pos:noun type:medicine")
    c1.close()

    c2 = db_mod.connect(db_path)
    try:
        # init_db on already-migrated DB must be a no-op for the value (AC6).
        db_mod.init_db(c2)
        assert vocab.get_focus_spec(c2, CHAT) == "pos:noun type:medicine", (
            f"focus_spec must survive bot restart; got "
            f"{vocab.get_focus_spec(c2, CHAT)!r}"
        )
    finally:
        c2.close()


def test_init_db_idempotent_with_focus_spec_column(
    tmp_path: Path,
) -> None:  # AC6 — re-running init_db on an already-migrated DB does not raise/wipe
    db_path = tmp_path / "vocab.db"
    c = db_mod.connect(db_path)
    try:
        db_mod.init_db(c)
        vocab.set_focus_spec(c, CHAT, "pos:noun")
        db_mod.init_db(c)  # second call must not raise
        db_mod.init_db(c)  # third call must not raise either
        assert vocab.get_focus_spec(c, CHAT) == "pos:noun", (
            "repeated init_db must not clobber existing focus_spec values"
        )
    finally:
        c.close()


# === vocab.select_word(names=...) ============================================


def test_select_word_with_names_filters_by_AND(
    conn: sqlite3.Connection,
) -> None:  # spec — names filters by AND across labels (AC3/AC4 substrate)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    vocab.add_word(conn, CHAT, "aspirin")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "horse", "type:animal")
    _attach(conn, CHAT, "pill", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")
    _attach(conn, CHAT, "aspirin", "pos:noun")
    _attach(conn, CHAT, "aspirin", "type:medicine")

    rng = random.Random(0)
    seen: set[str] = set()
    for _ in range(60):
        row = vocab.select_word(
            conn, CHAT, names=["pos:noun", "type:medicine"], rng=rng
        )
        assert row is not None, "filtered pool is non-empty; select_word must return a row"
        seen.add(row["text"])

    assert seen == {"pill", "aspirin"}, (
        f"AND across pos:noun + type:medicine should sample only pill/aspirin; got {sorted(seen)}"
    )


def test_select_word_empty_names_preserves_unfiltered(
    conn: sqlite3.Connection,
) -> None:  # spec — None / empty names preserves today's behaviour
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")

    seen_none: set[str] = set()
    seen_empty: set[str] = set()
    for _ in range(40):
        r1 = vocab.select_word(conn, CHAT, names=None, rng=random.Random())
        r2 = vocab.select_word(conn, CHAT, names=[], rng=random.Random())
        assert r1 is not None and r2 is not None
        seen_none.add(r1["text"])
        seen_empty.add(r2["text"])

    assert seen_none == {"alpha", "beta"}, (
        f"names=None must keep today's unfiltered pool; got {sorted(seen_none)}"
    )
    assert seen_empty == {"alpha", "beta"}, (
        f"names=[] must keep today's unfiltered pool; got {sorted(seen_empty)}"
    )


def test_select_word_zero_matching_names_returns_none(
    conn: sqlite3.Connection,
) -> None:  # AC3 / example — names match nothing → None (no exception)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")

    out = vocab.select_word(conn, CHAT, names=["type:medicine"], rng=random.Random(0))

    assert out is None, (
        f"select_word must return None when the filtered pool is empty; got {out!r}"
    )


# === scheduler.compose_push(names=...) =======================================


def test_compose_push_returns_none_when_filter_pool_empty(
    conn: sqlite3.Connection,
) -> None:  # AC3 + example — names that match nothing → None, llm not called
    import config_flow

    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings("UTC", 2, "09:00", "21:00", "funny", "ru"),
    )
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")

    async def llm_fail(msgs):  # must not be called when no candidate
        raise AssertionError("llm should not run when filtered pool is empty")

    out = asyncio.run(
        sched_module.compose_push(
            conn, CHAT, llm_chat=llm_fail, names=["type:medicine"], rng=random.Random(0)
        )
    )

    assert out is None, (
        f"compose_push must return None when filter yields no candidate; got {out!r}"
    )


def test_compose_push_passes_names_to_select_word(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — names is forwarded to vocab.select_word
    import config_flow

    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings("UTC", 2, "09:00", "21:00", "funny", "ru"),
    )
    vocab.add_word(conn, CHAT, "horse")

    seen_kwargs: list[dict] = []
    real_select = vocab.select_word

    def spy(conn_, chat_, **kw):
        seen_kwargs.append(kw)
        return real_select(conn_, chat_, **kw)

    monkeypatch.setattr(vocab, "select_word", spy)

    async def llm_ok(msgs):
        return "horse rides at dawn"

    asyncio.run(
        sched_module.compose_push(
            conn,
            CHAT,
            llm_chat=llm_ok,
            names=["pos:noun"],
            rng=random.Random(0),
        )
    )

    assert seen_kwargs, "select_word must be invoked at least once"
    assert seen_kwargs[0].get("names") == ["pos:noun"], (
        f"compose_push must forward names verbatim to select_word; got "
        f"{seen_kwargs[0].get('names')!r}"
    )


# === bot.cmd_focus ===========================================================


def test_cmd_focus_set_stores_normalized_spec_and_replies(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — /focus pos:noun type:medicine → stored + reply confirms
    _patch_bot(monkeypatch, conn)
    _seed_translatable(conn, CHAT, ["horse"])
    _attach(conn, CHAT, "horse", "pos:noun")
    # also attach type:medicine so AC5 zero-match warning is NOT triggered
    vocab.get_or_create_label(conn, CHAT, "type:medicine")
    _attach(conn, CHAT, "horse", "type:medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_focus(update, _make_context(["pos:noun", "type:medicine"])))

    assert vocab.get_focus_spec(conn, CHAT) == "pos:noun type:medicine", (
        f"normalized space-joined form must be stored; got "
        f"{vocab.get_focus_spec(conn, CHAT)!r}"
    )
    reply = _last_reply(update.message.reply_text)
    assert "pos:noun" in reply and "type:medicine" in reply, (
        f"reply must echo the stored focus tokens to confirm; got {reply!r}"
    )


def test_cmd_focus_set_idempotent_single_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — re-call leaves a single chat row with same value
    _patch_bot(monkeypatch, conn)
    _seed_translatable(conn, CHAT, ["horse"])
    _attach(conn, CHAT, "horse", "pos:noun")

    asyncio.run(bot.cmd_focus(_make_command_update(), _make_context(["pos:noun"])))
    asyncio.run(bot.cmd_focus(_make_command_update(), _make_context(["pos:noun"])))

    rows = conn.execute(
        "SELECT chat_id, focus_spec FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchall()
    assert len(rows) == 1, (
        f"second /focus must not duplicate the chat row; got {len(rows)}"
    )
    assert rows[0]["focus_spec"] == "pos:noun", (
        f"value must be stable across idempotent /focus; got {rows[0]['focus_spec']!r}"
    )


def test_cmd_focus_clear_token_clears_and_replies(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — /focus clear → NULL + "focus cleared"
    _patch_bot(monkeypatch, conn)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_focus(update, _make_context(["clear"])))

    assert vocab.get_focus_spec(conn, CHAT) is None, (
        f"/focus clear must NULL the column; got {vocab.get_focus_spec(conn, CHAT)!r}"
    )
    reply = _last_reply(update.message.reply_text).lower()
    assert "focus cleared" in reply, (
        f'AC2 mandates "focus cleared" wording; got {_last_reply(update.message.reply_text)!r}'
    )


def test_cmd_focus_clear_token_case_insensitive(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — single token "clear" is case-insensitive ("Clear", "CLEAR")
    _patch_bot(monkeypatch, conn)

    for token in ("Clear", "CLEAR", "cLeAr"):
        vocab.set_focus_spec(conn, CHAT, "pos:noun")
        assert vocab.get_focus_spec(conn, CHAT) == "pos:noun", "precondition"

        asyncio.run(bot.cmd_focus(_make_command_update(), _make_context([token])))

        assert vocab.get_focus_spec(conn, CHAT) is None, (
            f"/focus {token!r} must clear the column case-insensitively; "
            f"got {vocab.get_focus_spec(conn, CHAT)!r}"
        )


def test_cmd_focus_no_args_echoes_current_when_set(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — /focus (no args) echoes current spec
    _patch_bot(monkeypatch, conn)
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_focus(update, _make_context([])))

    reply = _last_reply(update.message.reply_text)
    assert "pos:noun" in reply and "type:medicine" in reply, (
        f"echo reply must contain the current focus tokens; got {reply!r}"
    )


def test_cmd_focus_no_args_replies_no_focus_set_when_unset(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — /focus (no args) → "no focus set" when NULL
    _patch_bot(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)  # row exists, but focus_spec is NULL

    update = _make_command_update()
    asyncio.run(bot.cmd_focus(update, _make_context([])))

    reply = _last_reply(update.message.reply_text).lower()
    assert "no focus set" in reply, (
        f'AC2 mandates "no focus set" wording when NULL; got '
        f'{_last_reply(update.message.reply_text)!r}'
    )


def test_cmd_focus_no_args_chat_missing_does_not_raise(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — /focus before /start (no chat row) must not raise
    _patch_bot(monkeypatch, conn)
    pre = conn.execute(
        "SELECT 1 FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    assert pre is None, "precondition: chat row must not yet exist"

    update = _make_command_update()
    # Must not raise even though the chat row is absent.
    asyncio.run(bot.cmd_focus(update, _make_context([])))

    reply = _last_reply(update.message.reply_text).lower()
    assert "no focus set" in reply, (
        f"missing chat row should still echo the unset state; got "
        f"{_last_reply(update.message.reply_text)!r}"
    )


def test_cmd_focus_zero_match_set_warns_but_persists(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — set succeeds but reply warns when pool currently empty
    _patch_bot(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)  # chat exists; zero words, zero matches

    update = _make_command_update()
    asyncio.run(bot.cmd_focus(update, _make_context(["pos:noun"])))

    # The column was still updated.
    assert vocab.get_focus_spec(conn, CHAT) == "pos:noun", (
        f"AC5: zero-match set is allowed (column updated); got "
        f"{vocab.get_focus_spec(conn, CHAT)!r}"
    )
    # And a warning surfaced.
    reply = _last_reply(update.message.reply_text)
    assert "⚠️" in reply or "no words match" in reply.lower(), (
        f"AC5: zero-match set must warn the user at set-time; got {reply!r}"
    )


def test_cmd_focus_malformed_spec_warning_no_db_write(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 / error — malformed spec → ⚠️ reply, no mutation
    _patch_bot(monkeypatch, conn)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")
    before = vocab.get_focus_spec(conn, CHAT)

    update = _make_command_update()
    asyncio.run(bot.cmd_focus(update, _make_context(["pos:"])))

    after = vocab.get_focus_spec(conn, CHAT)
    assert after == before, (
        f"AC7: malformed /focus must NOT mutate focus_spec; before={before!r}, after={after!r}"
    )
    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️"), (
        f"AC7: malformed reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "malformed label spec" in reply, (
        f'AC7: reply must echo the parser\'s "malformed label spec" prefix; got {reply!r}'
    )


def test_cmd_focus_dedupes_and_lowercases_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — duplicates / mixed case collapse via parse_label_spec
    _patch_bot(monkeypatch, conn)
    _seed_translatable(conn, CHAT, ["horse"])
    _attach(conn, CHAT, "horse", "pos:noun")

    asyncio.run(bot.cmd_focus(
        _make_command_update(),
        _make_context(["  Pos:Noun ", "pos:noun", "POS:NOUN"]),
    ))

    assert vocab.get_focus_spec(conn, CHAT) == "pos:noun", (
        f"duplicates and case must collapse to a single 'pos:noun'; got "
        f"{vocab.get_focus_spec(conn, CHAT)!r}"
    )


# === bot.dispatch_push =======================================================


def _seed_settings_for_dispatch(conn: sqlite3.Connection) -> None:
    """Minimum chat config so dispatch_push has something to operate on."""
    import config_flow

    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings("UTC", 2, "09:00", "21:00", "funny", "ru"),
    )


def _patch_dispatch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    *,
    compose_return,
) -> dict[str, MagicMock]:
    """Wire bot.conn + bot.app + intercept compose_push for dispatch tests."""
    monkeypatch.setattr(bot, "conn", conn)
    fake_app = MagicMock()
    fake_app.bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=999)
    )
    monkeypatch.setattr(bot, "app", fake_app)

    compose_spy = AsyncMock(return_value=compose_return)
    monkeypatch.setattr(bot.sched_module, "compose_push", compose_spy)

    return {"compose": compose_spy, "app": fake_app}


def test_dispatch_push_with_focus_passes_names_to_compose(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — dispatch_push reads focus and forwards as names=
    _seed_settings_for_dispatch(conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:medicine")

    spies = _patch_dispatch_runtime(
        monkeypatch, conn, compose_return=(1, "horse", "the horse runs")
    )

    asyncio.run(bot.dispatch_push(CHAT))

    assert spies["compose"].await_count == 1, (
        f"compose_push must be called exactly once; got {spies['compose'].await_count}"
    )
    kwargs = spies["compose"].await_args.kwargs
    assert kwargs.get("names") == ["pos:noun", "type:medicine"], (
        f"AC3: dispatch_push must forward focus names verbatim; got names="
        f"{kwargs.get('names')!r}"
    )


def test_dispatch_push_with_no_focus_passes_none(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — focus unset → names=None
    _seed_settings_for_dispatch(conn)
    vocab.add_word(conn, CHAT, "horse")
    assert vocab.get_focus_spec(conn, CHAT) is None  # precondition

    spies = _patch_dispatch_runtime(
        monkeypatch, conn, compose_return=(1, "horse", "the horse runs")
    )

    asyncio.run(bot.dispatch_push(CHAT))

    kwargs = spies["compose"].await_args.kwargs
    assert kwargs.get("names") is None, (
        f"AC3: no focus → names must be None (preserves today's behaviour); got "
        f"{kwargs.get('names')!r}"
    )


def test_dispatch_push_when_compose_returns_none_sends_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — empty filtered pool → no Telegram send, no exception
    _seed_settings_for_dispatch(conn)
    vocab.set_focus_spec(conn, CHAT, "type:medicine")  # focus that matches no words

    spies = _patch_dispatch_runtime(monkeypatch, conn, compose_return=None)

    # Must not raise.
    asyncio.run(bot.dispatch_push(CHAT))

    assert not spies["app"].bot.send_message.called, (
        "AC3: when compose_push returns None, dispatch_push must NOT push a message; "
        f"got send_message calls = {spies['app'].bot.send_message.call_args_list}"
    )


# === bot.on_play_game (focus seeding) ========================================


def test_on_play_game_seeds_filter_from_focus(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — focus → pending_game_filters[chat] seeded with parsed names
    _patch_bot(monkeypatch, conn)
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_callback_update("pg:start")
    asyncio.run(bot.on_play_game(update, _make_context([])))

    assert bot.pending_game_filters.get(CHAT) == ["pos:noun"], (
        f"AC4: on_play_game must seed pending_game_filters with the focus tokens; got "
        f"{bot.pending_game_filters!r}"
    )


def test_on_play_game_clears_filter_when_no_focus(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — no focus → dict entry cleared (not preserved from prior /games)
    _patch_bot(monkeypatch, conn)
    _seed_translatable(
        conn, CHAT, [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    )
    # Stale leftover from a prior /games <spec> that must NOT survive.
    bot.pending_game_filters[CHAT] = ["was-here"]
    assert vocab.get_focus_spec(conn, CHAT) is None  # precondition: no focus

    update = _make_callback_update("pg:start")
    asyncio.run(bot.on_play_game(update, _make_context([])))

    assert CHAT not in bot.pending_game_filters, (
        "AC4: when no focus is set, on_play_game must CLEAR the stash entry; "
        f"got {bot.pending_game_filters!r}"
    )


def test_on_play_game_then_picker_uses_focus_filtered_pool(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — end-to-end: focus → pg:start → gm:wt draws only from filtered pool
    _patch_bot(monkeypatch, conn)
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    other = [f"o{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching + other)
    for w in matching:
        _attach(conn, CHAT, w, "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")
    matching_ids = {_word_id(conn, CHAT, w) for w in matching}

    # Step 1: tap "🎮 Play game" — this should seed the focus into the stash.
    pg_update = _make_callback_update("pg:start")
    asyncio.run(bot.on_play_game(pg_update, _make_context([])))
    assert bot.pending_game_filters.get(CHAT) == ["pos:noun"], (
        "precondition: on_play_game must seed the stash before picker tap; got "
        f"{bot.pending_game_filters!r}"
    )

    # Step 2: tap a direction button — the same on_games_menu path #85 wired up
    # must consume the seeded stash and draw rounds from the filtered pool only.
    gm_update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(gm_update, _make_context([])))

    game = bot.games.get(CHAT)
    assert game is not None, (
        "AC4: a viable focus-filtered pool must start a game on the picker tap"
    )
    round_ids = {r.word_id for r in game.rounds}
    assert round_ids, "AC4: started game must have at least one round"
    assert round_ids.issubset(matching_ids), (
        "AC4: rounds must be drawn ONLY from the focus-filtered pool; "
        f"unexpected ids leaked = {round_ids - matching_ids}"
    )


# === COMMANDS surface / handler registration ================================


def test_focus_in_COMMANDS_with_description() -> None:  # AC8 — entry present
    desc = next((d for n, d in bot.COMMANDS if n == "focus"), None)
    assert desc is not None, (
        f"AC8: COMMANDS must contain a 'focus' entry; got names="
        f"{[n for n, _ in bot.COMMANDS]!r}"
    )
    assert desc.strip(), f"AC8: 'focus' description must be non-empty; got {desc!r}"


def test_focus_handler_registered_with_command_handler() -> None:  # AC8 — main() wires CommandHandler
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert (
        'CommandHandler("focus", cmd_focus)' in src
        or "CommandHandler('focus', cmd_focus)" in src
    ), (
        "AC8: bot.main() must register CommandHandler(\"focus\", cmd_focus)"
    )
