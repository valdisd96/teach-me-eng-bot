"""Tests for issue #97 — label suggestions after /add.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. The
public surface under test:

- `label_suggestor.parse_llm_response` — pure JSON parser (POS + labels).
- `label_suggestor.suggest_labels` — async; wraps the LLM call, swallows
  errors, dedupes/caps the result.
- `label_suggestor.PendingLabel` — bounded LRU token registry, mirrors
  `translator.PendingVocab`.
- `bot._send_label_suggestions` — best-effort follow-up after a successful
  `/add`: posts one inline button per suggestion via `app.bot.send_message`.
- `bot.cmd_add` — must NOT invoke the suggestion path on the duplicate branch.
- `bot.on_label_suggest` — handles the `lbl:<token>` callback: attach label,
  edit the tapped button to `✅ <name>` (or `⚠️ expired` on miss).

Telegram Update/Context is mocked at the seam (AsyncMock + MagicMock); same
shape as `tests/test_play_game_button.py` and `tests/test_label_commands.py`.
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
import label_suggestor  # noqa: E402
import vocab  # noqa: E402


CHAT = 9999


# === parse_llm_response (pure) ==============================================


def test_parse_llm_response_pos_plus_existing_labels() -> None:  # AC7 + example
    raw = '{"pos": "noun", "labels": ["type:animal", "type:pet"]}'
    existing = ["type:animal", "type:pet", "type:medicine"]
    out = label_suggestor.parse_llm_response(raw, existing)
    assert out == ["pos:noun", "type:animal", "type:pet"], (
        f"POS first then existing labels in declared order; got {out!r}"
    )


def test_parse_llm_response_filters_unknown_keeps_pos() -> None:  # AC7 + example
    raw = '{"pos": "noun", "labels": ["type:invented"]}'
    existing = ["type:animal"]
    out = label_suggestor.parse_llm_response(raw, existing)
    assert out == ["pos:noun"], (
        "non-existing label must be filtered, but POS:* may be brand-new (AC7); "
        f"got {out!r}"
    )


def test_parse_llm_response_garbage_returns_empty() -> None:  # example: garbage → []
    assert label_suggestor.parse_llm_response("garbage", []) == [], (
        "non-JSON noise must yield []"
    )


def test_parse_llm_response_normalises_whitespace_case() -> None:  # edge: whitespace/mixed-case
    raw = '{"pos": "Noun", "labels": ["  Type:Animal  "]}'
    existing = ["type:animal"]
    out = label_suggestor.parse_llm_response(raw, existing)
    assert out == ["pos:noun", "type:animal"], (
        f"whitespace/mixed-case must be normalised before lookup; got {out!r}"
    )


def test_parse_llm_response_dedupes_pos_repeats() -> None:  # edge: duplicates preserve-order
    raw = '{"pos": "noun", "labels": ["pos:noun", "type:animal", "type:animal"]}'
    existing = ["type:animal", "pos:noun"]
    out = label_suggestor.parse_llm_response(raw, existing)
    # First occurrence wins for both pos:noun and type:animal.
    assert out == ["pos:noun", "type:animal"], (
        f"duplicates must be collapsed preserving first-occurrence order; got {out!r}"
    )


def test_parse_llm_response_non_json_noise_returns_empty() -> None:  # edge: malformed JSON
    # Several flavours of broken/non-JSON input must all yield [].
    assert label_suggestor.parse_llm_response("", ["type:x"]) == []
    assert label_suggestor.parse_llm_response("not even close", ["type:x"]) == []
    assert label_suggestor.parse_llm_response('{"pos": "noun", ', ["type:x"]) == []


# === suggest_labels (async) =================================================


def _llm_returning(text: str) -> AsyncMock:
    """An llm_chat-shaped AsyncMock that returns `text`."""
    return AsyncMock(return_value=text)


def _llm_raising(exc: Exception) -> AsyncMock:
    return AsyncMock(side_effect=exc)


def test_suggest_labels_returns_pos_first() -> None:  # AC2 — POS first when present
    llm = _llm_returning('{"pos": "noun", "labels": ["type:animal", "type:pet"]}')
    out = asyncio.run(label_suggestor.suggest_labels(
        "horse", ["type:animal", "type:pet"], llm
    ))
    assert out and out[0] == "pos:noun", (
        f"first element must be the POS suggestion; got {out!r}"
    )


def test_suggest_labels_caps_at_max_suggestions() -> None:  # AC8 — cap, POS counts toward it
    existing = ["type:a", "type:b", "type:c", "type:d", "type:e", "type:f"]
    raw = (
        '{"pos": "noun", '
        '"labels": ["type:a", "type:b", "type:c", "type:d", "type:e", "type:f"]}'
    )
    llm = _llm_returning(raw)
    out = asyncio.run(label_suggestor.suggest_labels(
        "horse", existing, llm, max_suggestions=3
    ))
    assert len(out) == 3, f"cap not respected; got {out!r}"
    assert out[0] == "pos:noun", (
        f"POS must be present and first when within cap; got {out!r}"
    )
    # POS counts toward the cap → only 2 non-POS labels survive.
    assert out == ["pos:noun", "type:a", "type:b"], (
        f"POS must count toward the cap (AC8); got {out!r}"
    )


def test_suggest_labels_returns_empty_on_llm_exception() -> None:  # AC6 + error: llm raises
    llm = _llm_raising(RuntimeError("upstream model down"))
    out = asyncio.run(label_suggestor.suggest_labels("horse", ["type:x"], llm))
    assert out == [], (
        f"LLM error must produce [] without raising (AC6); got {out!r}"
    )


def test_suggest_labels_returns_empty_on_unparseable() -> None:  # edge: malformed reply
    llm = _llm_returning("not-json-at-all")
    out = asyncio.run(label_suggestor.suggest_labels("horse", ["type:x"], llm))
    assert out == [], f"unparseable reply must yield []; got {out!r}"


def test_suggest_labels_empty_existing_with_pos_only() -> None:  # edge: empty existing + POS works
    llm = _llm_returning('{"pos": "noun", "labels": []}')
    out = asyncio.run(label_suggestor.suggest_labels("horse", [], llm))
    assert out == ["pos:noun"], (
        "empty existing_labels must still yield the POS suggestion; "
        f"got {out!r}"
    )


def test_suggest_labels_empty_existing_and_llm_fails() -> None:  # edge: empty existing + fail
    llm = _llm_raising(RuntimeError("nope"))
    out = asyncio.run(label_suggestor.suggest_labels("horse", [], llm))
    assert out == [], f"empty existing + LLM failure must yield []; got {out!r}"


# === PendingLabel registry ==================================================


def test_pending_label_register_returns_unique_monotonic_tokens() -> None:  # AC9
    pl = label_suggestor.PendingLabel()
    t1 = pl.register(1, 10, "pos:noun")
    t2 = pl.register(1, 10, "type:animal")
    t3 = pl.register(2, 20, "type:pet")
    assert len({t1, t2, t3}) == 3, f"tokens must be unique; got {(t1, t2, t3)!r}"
    assert t1 < t2 < t3, (
        f"tokens must be strictly increasing (AC9); got {(t1, t2, t3)!r}"
    )


def test_pending_label_pop_returns_entry_and_removes_it() -> None:  # AC9
    pl = label_suggestor.PendingLabel()
    token = pl.register(42, 7, "type:animal")
    assert pl.pop(token) == (42, 7, "type:animal"), (
        "pop must return the registered tuple"
    )
    assert pl.pop(token) is None, "second pop of the same token must be None (AC9)"


def test_pending_label_pop_unknown_returns_none() -> None:  # AC9
    pl = label_suggestor.PendingLabel()
    assert pl.pop(123456) is None


def test_pending_label_evicts_oldest_past_cap() -> None:  # AC9
    pl = label_suggestor.PendingLabel(max_size=3)
    t1 = pl.register(1, 10, "a")
    t2 = pl.register(1, 11, "b")
    t3 = pl.register(1, 12, "c")
    t4 = pl.register(1, 13, "d")  # evicts t1
    assert pl.pop(t1) is None, "oldest entry must be evicted past cap"
    assert pl.pop(t2) == (1, 11, "b")
    assert pl.pop(t3) == (1, 12, "c")
    assert pl.pop(t4) == (1, 13, "d")


def test_pending_label_len_tracks_live_entries() -> None:  # AC9
    pl = label_suggestor.PendingLabel()
    assert len(pl) == 0
    token = pl.register(1, 10, "a")
    assert len(pl) == 1
    pl.pop(token)
    assert len(pl) == 0


# === bot._send_label_suggestions ============================================


def _make_app_spy() -> tuple[MagicMock, AsyncMock]:
    """Returns (app_mock, send_message_spy). The app_mock is what bot.app is set to."""
    app_mock = MagicMock()
    send_spy = AsyncMock()
    app_mock.bot.send_message = send_spy
    return app_mock, send_spy


def _seed_word_with_existing_labels(
    conn: sqlite3.Connection, word: str, label_names: list[str]
) -> int:
    vocab.add_word(conn, CHAT, word)
    word_id = vocab.find_word_id(conn, CHAT, word)
    assert word_id is not None
    for name in label_names:
        # Create the label rows but do NOT attach them to the word — the
        # follow-up flow reads them via labels_with_counts (chat-scoped).
        vocab.get_or_create_label(conn, CHAT, name)
    return word_id


def _kbd_buttons(reply_markup) -> list:
    if reply_markup is None:
        return []
    rows = getattr(reply_markup, "inline_keyboard", None) or []
    return [btn for row in rows for btn in row]


def _kbd_rows(reply_markup) -> list:
    if reply_markup is None:
        return []
    return list(getattr(reply_markup, "inline_keyboard", None) or [])


def test_send_label_suggestions_sends_one_message_one_button_per_suggestion(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — exactly one extra message, one button per suggestion
    monkeypatch.setattr(bot, "conn", conn)
    app_mock, send_spy = _make_app_spy()
    monkeypatch.setattr(bot, "app", app_mock)

    word_id = _seed_word_with_existing_labels(conn, "horse", ["type:animal", "type:pet"])
    monkeypatch.setattr(
        bot.label_suggestor,
        "suggest_labels",
        AsyncMock(return_value=["pos:noun", "type:animal", "type:pet"]),
    )

    asyncio.run(bot._send_label_suggestions(CHAT, word_id, "horse"))

    assert send_spy.await_count == 1, (
        f"exactly one extra message expected (AC1); got {send_spy.await_count}"
    )
    call = send_spy.await_args
    assert call.kwargs.get("chat_id") == CHAT, (
        f"send_message must target the same chat; kwargs={call.kwargs!r}"
    )
    btns = _kbd_buttons(call.kwargs.get("reply_markup"))
    assert len(btns) == 3, (
        f"one button per suggestion (AC1); got {[b.text for b in btns]!r}"
    )
    assert [b.text for b in btns] == ["pos:noun", "type:animal", "type:pet"], (
        f"button text must equal the suggested label name; got {[b.text for b in btns]!r}"
    )
    for b in btns:
        assert b.callback_data.startswith("lbl:"), (
            f"each button's callback_data must start with 'lbl:'; got {b.callback_data!r}"
        )


def test_send_label_suggestions_one_row_grid(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — buttons laid out as a 1-row grid (one button per row OR all in one row)
    monkeypatch.setattr(bot, "conn", conn)
    app_mock, send_spy = _make_app_spy()
    monkeypatch.setattr(bot, "app", app_mock)

    word_id = _seed_word_with_existing_labels(conn, "horse", ["type:animal", "type:pet"])
    monkeypatch.setattr(
        bot.label_suggestor,
        "suggest_labels",
        AsyncMock(return_value=["pos:noun", "type:animal", "type:pet"]),
    )

    asyncio.run(bot._send_label_suggestions(CHAT, word_id, "horse"))

    rows = _kbd_rows(send_spy.await_args.kwargs.get("reply_markup"))
    # The spec says "1-row grid" — accept either layout interpretation:
    #   (a) a single horizontal row with N buttons, or
    #   (b) N rows of one button each (each button on its own row).
    # Both are unambiguously "one button per row position" with no 2-D grid.
    is_single_row = len(rows) == 1 and len(rows[0]) == 3
    is_one_per_row = len(rows) == 3 and all(len(r) == 1 for r in rows)
    assert is_single_row or is_one_per_row, (
        f"AC1 says 1-row grid: either one row of N buttons or N rows of 1 button each; "
        f"got rows={[len(r) for r in rows]!r}"
    )


def test_send_label_suggestions_pos_button_first(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — POS button is the first button in the keyboard
    monkeypatch.setattr(bot, "conn", conn)
    app_mock, send_spy = _make_app_spy()
    monkeypatch.setattr(bot, "app", app_mock)

    word_id = _seed_word_with_existing_labels(conn, "horse", ["type:animal", "type:pet"])
    monkeypatch.setattr(
        bot.label_suggestor,
        "suggest_labels",
        AsyncMock(return_value=["pos:noun", "type:animal", "type:pet"]),
    )

    asyncio.run(bot._send_label_suggestions(CHAT, word_id, "horse"))

    btns = _kbd_buttons(send_spy.await_args.kwargs.get("reply_markup"))
    assert btns and btns[0].text == "pos:noun", (
        f"POS button must be first (AC2); got {[b.text for b in btns]!r}"
    )


def test_send_label_suggestions_no_message_when_suggest_returns_empty(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — empty suggestions → no follow-up message
    monkeypatch.setattr(bot, "conn", conn)
    app_mock, send_spy = _make_app_spy()
    monkeypatch.setattr(bot, "app", app_mock)

    word_id = _seed_word_with_existing_labels(conn, "horse", ["type:animal"])
    monkeypatch.setattr(
        bot.label_suggestor, "suggest_labels", AsyncMock(return_value=[])
    )

    asyncio.run(bot._send_label_suggestions(CHAT, word_id, "horse"))

    send_spy.assert_not_called()


def test_send_label_suggestions_no_message_when_llm_raises(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 + error: suggest_labels raises → no follow-up, no propagation
    monkeypatch.setattr(bot, "conn", conn)
    app_mock, send_spy = _make_app_spy()
    monkeypatch.setattr(bot, "app", app_mock)

    word_id = _seed_word_with_existing_labels(conn, "horse", ["type:animal"])
    monkeypatch.setattr(
        bot.label_suggestor,
        "suggest_labels",
        AsyncMock(side_effect=RuntimeError("flaky model")),
    )

    # Must not raise.
    asyncio.run(bot._send_label_suggestions(CHAT, word_id, "horse"))

    send_spy.assert_not_called()


def test_send_label_suggestions_swallows_telegram_send_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # error: TG send failure caught
    monkeypatch.setattr(bot, "conn", conn)
    app_mock = MagicMock()
    app_mock.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    monkeypatch.setattr(bot, "app", app_mock)

    word_id = _seed_word_with_existing_labels(conn, "horse", ["type:animal"])
    monkeypatch.setattr(
        bot.label_suggestor,
        "suggest_labels",
        AsyncMock(return_value=["pos:noun"]),
    )

    # Must not raise — user-facing /add reply has already been sent.
    asyncio.run(bot._send_label_suggestions(CHAT, word_id, "horse"))


# === bot.cmd_add — duplicate-word path =====================================


def _make_command_update(text: str, chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str]) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    return ctx


def test_cmd_add_duplicate_word_does_not_send_suggestions(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — duplicate /add → no suggestion message
    monkeypatch.setattr(bot, "conn", conn)
    app_mock, send_spy = _make_app_spy()
    monkeypatch.setattr(bot, "app", app_mock)
    # Pre-seed: word already exists, so cmd_add hits the duplicate branch.
    vocab.add_word(conn, CHAT, "horse")

    # Defensive: if cmd_add accidentally runs the suggestion path despite the
    # duplicate, this would still be called — we want to detect that.
    suggest_spy = AsyncMock(return_value=["pos:noun"])
    monkeypatch.setattr(bot.label_suggestor, "suggest_labels", suggest_spy)

    update = _make_command_update("/add horse")
    asyncio.run(bot.cmd_add(update, _make_context(["horse"])))

    # User-facing reply should be the duplicate message — verifies branch.
    reply_calls = update.message.reply_text.call_args_list
    assert reply_calls, "cmd_add must reply to the user"
    last = reply_calls[-1]
    last_text = last.args[0] if last.args else last.kwargs.get("text", "")
    assert "Already in your vocab" in last_text, (
        f"duplicate path must produce 'Already in your vocab' reply; got {last_text!r}"
    )
    # Crucially: no suggestion follow-up was sent.
    send_spy.assert_not_called()
    suggest_spy.assert_not_called()


# === bot.on_label_suggest ===================================================


def _make_callback_update(callback_data: str, chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    # Default keyboard the bot will edit: a single button matching the tap.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    update.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("pos:noun", callback_data=callback_data)]]
    )
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    return update


def _new_kbd_from_edit(update: MagicMock):
    """Pull the InlineKeyboardMarkup out of the most recent edit call.

    The handler may use either `query.edit_message_reply_markup(reply_markup=…)`
    or `query.message.edit_reply_markup(reply_markup=…)`; accept either.
    """
    candidates = [
        update.callback_query.edit_message_reply_markup,
        update.callback_query.message.edit_reply_markup,
    ]
    for spy in candidates:
        if spy.call_args is not None:
            kw = spy.call_args.kwargs
            if "reply_markup" in kw:
                return kw["reply_markup"]
            if spy.call_args.args:
                return spy.call_args.args[0]
    raise AssertionError(
        "expected an edit_*_reply_markup call carrying the new keyboard"
    )


def test_on_label_suggest_attaches_label_and_marks_button(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — tap attaches label + edits button to ✅ <name>, callback_data → noop
    monkeypatch.setattr(bot, "conn", conn)
    # Fresh PendingLabel so the test owns the token namespace.
    monkeypatch.setattr(bot, "pending_labels", label_suggestor.PendingLabel())

    vocab.add_word(conn, CHAT, "horse")
    word_id = vocab.find_word_id(conn, CHAT, "horse")
    assert word_id is not None
    token = bot.pending_labels.register(CHAT, word_id, "pos:noun")

    update = _make_callback_update(f"lbl:{token}")
    asyncio.run(bot.on_label_suggest(update, MagicMock()))

    # Label is attached (auto-creates the row via get_or_create_label).
    assert vocab.labels_for_word(conn, word_id) == ["pos:noun"], (
        f"AC3 — label must be attached to the word; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )
    # Tapped button is now ✅, callback_data is noop.
    new_kbd = _new_kbd_from_edit(update)
    btns = [b for row in new_kbd.inline_keyboard for b in row]
    matched = [b for b in btns if b.text == "✅ pos:noun"]
    assert matched, (
        f"AC3 — tapped button must be replaced by '✅ pos:noun'; "
        f"got {[b.text for b in btns]!r}"
    )
    assert matched[0].callback_data == "noop", (
        f"AC3 — tapped button callback_data must become 'noop'; "
        f"got {matched[0].callback_data!r}"
    )


def test_on_label_suggest_double_tap_no_db_change(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — second tap on the same token: no DB change, no duplication
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "pending_labels", label_suggestor.PendingLabel())

    vocab.add_word(conn, CHAT, "horse")
    word_id = vocab.find_word_id(conn, CHAT, "horse")
    assert word_id is not None
    token = bot.pending_labels.register(CHAT, word_id, "pos:noun")

    # First tap: attaches.
    asyncio.run(bot.on_label_suggest(_make_callback_update(f"lbl:{token}"), MagicMock()))
    after_first = vocab.labels_for_word(conn, word_id)
    rows_first = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels WHERE word_id = ?", (word_id,)
    ).fetchone()["n"]

    # Second tap on the same (now-popped) token: must not double-write.
    asyncio.run(bot.on_label_suggest(_make_callback_update(f"lbl:{token}"), MagicMock()))
    after_second = vocab.labels_for_word(conn, word_id)
    rows_second = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels WHERE word_id = ?", (word_id,)
    ).fetchone()["n"]

    assert after_first == ["pos:noun"], (
        f"first tap should have attached pos:noun; got {after_first!r}"
    )
    assert after_second == after_first, (
        f"AC4 — second tap must not change attached labels; "
        f"first={after_first!r}, second={after_second!r}"
    )
    assert rows_second == rows_first, (
        f"AC4 — second tap must not insert another word_labels row; "
        f"first={rows_first}, second={rows_second}"
    )


def test_on_label_suggest_unknown_token_marks_expired(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC10 — unknown token → ⚠️ expired, no DB write
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "pending_labels", label_suggestor.PendingLabel())

    vocab.add_word(conn, CHAT, "horse")
    word_id = vocab.find_word_id(conn, CHAT, "horse")
    assert word_id is not None
    pre_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels WHERE word_id = ?", (word_id,)
    ).fetchone()["n"]

    # Token 999 was never registered → "expired".
    update = _make_callback_update("lbl:999")
    asyncio.run(bot.on_label_suggest(update, MagicMock()))

    post_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels WHERE word_id = ?", (word_id,)
    ).fetchone()["n"]
    assert post_rows == pre_rows, (
        f"AC10 — unknown token must perform no DB write; rows {pre_rows} → {post_rows}"
    )

    new_kbd = _new_kbd_from_edit(update)
    btns = [b for row in new_kbd.inline_keyboard for b in row]
    expired = [b for b in btns if b.text == "⚠️ expired"]
    assert expired, (
        f"AC10 — tapped button must read '⚠️ expired'; "
        f"got {[b.text for b in btns]!r}"
    )


def test_on_label_suggest_handler_registered_with_lbl_pattern() -> None:  # AC1 — handler wiring
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert (
        'CallbackQueryHandler(on_label_suggest, pattern=r"^lbl:")' in src
        or "CallbackQueryHandler(on_label_suggest, pattern=r'^lbl:')" in src
    ), "main() must register CallbackQueryHandler(on_label_suggest, pattern=r'^lbl:')"
