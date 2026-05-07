"""Tests for issue #84 — `/list` filter accepts a label spec (AND semantics).

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. Behaviour
asserted here is derived from that spec, not from `cmd_list`'s body.

Mocking shape mirrors `tests/test_label_commands.py` — Telegram Update / Context
are MagicMock + AsyncMock at the architectural seam, with `bot.conn` patched to
the temp-DB fixture.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import vocab  # noqa: E402


CHAT = 8484


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
    return ctx


def _patch_conn(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)


def _last_reply(update: MagicMock) -> str:
    calls = update.message.reply_text.call_args_list
    assert calls, "expected at least one reply_text call"
    last = calls[-1]
    if last.args:
        return last.args[0]
    return last.kwargs["text"]


def _word_id(conn: sqlite3.Connection, chat_id: int, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat_id, text)
    ).fetchone()["id"]


def _attach(conn: sqlite3.Connection, chat_id: int, word: str, label: str) -> None:
    wid = _word_id(conn, chat_id, word)
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, chat_id, label))


# === unfiltered branch (AC2) ================================================


def test_cmd_list_no_args_unfiltered_format(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — unfiltered output keeps today's per-row format, no label suffix
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    # Attach labels — they MUST NOT bleed into unfiltered output.
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    # Per-row format from spec: "• <text> (seen N×, score S)".
    for word in ("horse", "pill"):
        assert f"• {word} (seen " in reply, (
            f"unfiltered row format must be '• {word} (seen N×, score S)'; got {reply!r}"
        )
        assert "score " in reply, f"row format must include 'score'; got {reply!r}"
    # Unfiltered output must not append the labels suffix introduced for the filtered branch.
    assert " — " not in reply, (
        "unfiltered rows must NOT carry the filtered branch's '— labels…' suffix; "
        f"got {reply!r}"
    )


def test_cmd_list_no_args_ordering_mention_count_then_recent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — ordering preserved: mention_count ASC, added_at DESC
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "old_high")
    vocab.add_word(conn, CHAT, "old_low")
    vocab.add_word(conn, CHAT, "new_low")
    conn.execute("UPDATE words SET mention_count = 5 WHERE text = 'old_high'")
    conn.execute(
        "UPDATE words SET added_at = '2020-01-01T00:00:00+00:00' WHERE text = 'old_low'"
    )

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    pos_new_low = reply.index("new_low")
    pos_old_low = reply.index("old_low")
    pos_old_high = reply.index("old_high")
    assert pos_new_low < pos_old_low < pos_old_high, (
        "expected mention_count ASC then added_at DESC: "
        f"new_low, old_low, old_high; got order in {reply!r}"
    )


def test_cmd_list_no_args_empty_chat_message(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — empty chat + no args → today's "Your vocab is empty…" message
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    assert "vocab is empty" in reply.lower(), (
        f'unfiltered empty-chat reply must say "vocab is empty"; got {reply!r}'
    )
    # And it must NOT be the filtered branch's "no words match those labels" reply.
    assert "no words match those labels" not in reply, (
        f"unfiltered empty path must keep its own message, not the filtered one; got {reply!r}"
    )


# === filtered branch — AND semantics (AC1) ==================================


def test_cmd_list_filter_and_semantics_across_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — /list pos:noun type:medicine returns intersection only
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    vocab.add_word(conn, CHAT, "aspirin")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "horse", "type:animal")
    _attach(conn, CHAT, "pill", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")
    _attach(conn, CHAT, "aspirin", "pos:noun")
    _attach(conn, CHAT, "aspirin", "type:medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun", "type:medicine"])))

    reply = _last_reply(update)
    assert "pill" in reply, f"AND-match 'pill' must appear; got {reply!r}"
    assert "aspirin" in reply, f"AND-match 'aspirin' must appear; got {reply!r}"
    assert "horse" not in reply, (
        "'horse' has pos:noun but not type:medicine — must be excluded by AND semantics; "
        f"got {reply!r}"
    )


# === filtered branch — empty / unknown label messages (AC3, edges) ==========


def test_cmd_list_filter_zero_matches_replies_no_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — filtered + zero matches → "no words match those labels"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")  # has pos:noun, lacks type:medicine

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun", "type:medicine"])))

    reply = _last_reply(update)
    assert reply == "no words match those labels", (
        f'AC3 mandates exact "no words match those labels"; got {reply!r}'
    )


def test_cmd_list_filter_unknown_label_replies_no_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — label not present in this chat → same "no words match those labels"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["type:plant"])))

    reply = _last_reply(update)
    assert reply == "no words match those labels", (
        f'unknown-label filter must reply "no words match those labels"; got {reply!r}'
    )


def test_cmd_list_filter_empty_chat_replies_no_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — chat has zero words + filter → "no words match those labels"
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun"])))

    reply = _last_reply(update)
    assert reply == "no words match those labels", (
        "filtered branch on an empty chat must use the filtered no-match message, "
        f"not the unfiltered empty-vocab message; got {reply!r}"
    )


# === filtered branch — per-row label suffix (AC4) ===========================


def test_cmd_list_filter_rows_show_label_suffix(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — filtered rows append "— <labels>" so user sees why each matched
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "horse", "type:animal")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun"])))

    reply = _last_reply(update)
    # Find the line for "horse".
    line = next((ln for ln in reply.splitlines() if "horse" in ln), None)
    assert line is not None, f"horse row missing; got {reply!r}"
    assert " — " in line, (
        f"filtered row must end with the '— labels' suffix per AC4 example; got {line!r}"
    )
    assert "pos:noun" in line, f"row must list 'pos:noun'; got {line!r}"
    assert "type:animal" in line, f"row must list 'type:animal'; got {line!r}"


def test_cmd_list_filter_unfiltered_rows_no_label_suffix(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — unfiltered output stays untouched (no label suffix on rows)
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")  # word DOES have a label
    _attach(conn, CHAT, "horse", "type:animal")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))  # unfiltered

    reply = _last_reply(update)
    line = next((ln for ln in reply.splitlines() if "horse" in ln), None)
    assert line is not None, f"horse row missing; got {reply!r}"
    assert " — " not in line, (
        "unfiltered rows must NOT carry the '— labels' suffix even when the word has labels; "
        f"got {line!r}"
    )


# === malformed spec (AC5 + error condition) =================================


def test_cmd_list_malformed_spec_warning_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — malformed → "⚠️ malformed label spec: <bad tokens>"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([":foo"])))

    reply = _last_reply(update)
    assert reply.startswith("⚠️ "), (
        f'malformed spec must yield a "⚠️ "-prefixed reply per AC5; got {reply!r}'
    )
    assert "malformed label spec" in reply, (
        f'reply must include "malformed label spec" from the shared parser; got {reply!r}'
    )
    assert ":foo" in reply, f"reply must echo the offending token; got {reply!r}"


def test_cmd_list_malformed_spec_does_not_query_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — malformed spec performs no DB read of words
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")

    def _boom(*args, **kwargs):  # any read of words must fail loudly
        raise AssertionError("words should not be queried on a malformed spec")

    monkeypatch.setattr(vocab, "list_words", _boom)
    monkeypatch.setattr(vocab, "words_matching_labels", _boom)

    update = _make_command_update()
    # Must not raise — handler must short-circuit before either call.
    asyncio.run(bot.cmd_list(update, _make_context(["a:b:c"])))

    reply = _last_reply(update)
    assert reply.startswith("⚠️ "), (
        f"⚠️-prefixed reply expected on malformed spec; got {reply!r}"
    )


# === edges =================================================================


def test_cmd_list_filter_dedupes_duplicate_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — duplicate tokens dedupe; behaves like single-token
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    _attach(conn, CHAT, "horse", "pos:noun")
    # 'pill' has no pos:noun.

    update_dup = _make_command_update()
    asyncio.run(bot.cmd_list(update_dup, _make_context(["pos:noun", "pos:noun"])))
    update_single = _make_command_update()
    asyncio.run(bot.cmd_list(update_single, _make_context(["pos:noun"])))

    reply_dup = _last_reply(update_dup)
    reply_single = _last_reply(update_single)

    # Same set of words appears in both, and 'pill' is excluded in both.
    assert "horse" in reply_dup, f"horse must appear with duplicates; got {reply_dup!r}"
    assert "horse" in reply_single, f"horse must appear with single token; got {reply_single!r}"
    assert "pill" not in reply_dup, (
        f"pill (no pos:noun) must not appear with duplicates; got {reply_dup!r}"
    )
    assert "pill" not in reply_single, (
        f"pill (no pos:noun) must not appear with single token; got {reply_single!r}"
    )


def test_cmd_list_filter_single_bare_token_is_label(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — "/list medicine" is a label spec, NOT a substring filter
    _patch_conn(monkeypatch, conn)
    # A word whose TEXT contains "medicine" but who has no such label attached.
    vocab.add_word(conn, CHAT, "medicine")
    # A different word that DOES carry the bare-string label "medicine".
    vocab.add_word(conn, CHAT, "aspirin")
    _attach(conn, CHAT, "aspirin", "medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["medicine"])))

    reply = _last_reply(update)
    assert "aspirin" in reply, (
        f"label-tagged word 'aspirin' must appear (label match, not substring); got {reply!r}"
    )
    # The word *literally named* 'medicine' has no label 'medicine' attached, so it must NOT match.
    line_with_medicine_word = next(
        (ln for ln in reply.splitlines() if ln.startswith("• medicine ")), None
    )
    assert line_with_medicine_word is None, (
        "bare token must be treated as a label spec, not a substring filter — "
        f"the untagged word literally named 'medicine' must be excluded; got {reply!r}"
    )


def test_cmd_list_filter_normalizes_whitespace_and_case(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — "  Pos:Noun " normalises via shared parser; same as "pos:noun"
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")

    update_canon = _make_command_update()
    asyncio.run(bot.cmd_list(update_canon, _make_context(["pos:noun"])))
    update_messy = _make_command_update()
    asyncio.run(bot.cmd_list(update_messy, _make_context(["  Pos:Noun  "])))

    reply_canon = _last_reply(update_canon)
    reply_messy = _last_reply(update_messy)
    assert "horse" in reply_canon, f"baseline match must hit; got {reply_canon!r}"
    assert "horse" in reply_messy, (
        "whitespace + casing in the spec token must normalise to the canonical form; "
        f"got {reply_messy!r}"
    )
