"""Tests for issue #118 — `/list` pagination, per-row labels everywhere, `--any` OR-mode.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. All
behaviour asserted here derives from that spec, not from `cmd_list`'s body —
each test carries the AC ID (or `# edge:` / `# error:`) it traces back to.

Telegram Update / Context is mocked at the seam (AsyncMock + MagicMock); the
project's other handler tests (test_list_label_spec.py, test_label_commands.py)
follow the same shape.
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


CHAT = 8485


# -- helpers (mirror tests/test_list_label_spec.py) --------------------------


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


def _all_replies(update: MagicMock) -> list[str]:
    out: list[str] = []
    for call in update.message.reply_text.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _last_reply(update: MagicMock) -> str:
    replies = _all_replies(update)
    assert replies, "expected at least one reply_text call"
    return replies[-1]


def _word_id(conn: sqlite3.Connection, chat_id: int, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat_id, text)
    ).fetchone()["id"]


def _attach(conn: sqlite3.Connection, chat_id: int, word: str, label: str) -> None:
    wid = _word_id(conn, chat_id, word)
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, chat_id, label))


# === _chunk_lines (pure helper, public API in spec) =========================


def test_chunk_lines_empty_lines_returns_header_only() -> None:
    # edge: empty `lines` → single chunk holding just the header
    result = bot._chunk_lines([], 100, "Vocab (0):")
    assert result == ["Vocab (0):"], (
        f"empty lines must yield a single header-only chunk per spec; got {result!r}"
    )


def test_chunk_lines_first_chunk_starts_with_header() -> None:
    # AC1 mechanic: when there's content the first chunk begins with `header + "\n"`
    result = bot._chunk_lines(["a", "b"], 100, "H")
    assert result[0].startswith("H\n"), (
        f"first chunk must start with 'header\\n' per spec; got {result[0]!r}"
    )


def test_chunk_lines_later_chunks_have_no_header() -> None:
    # AC1 mechanic: subsequent chunks contain only line content (no header)
    lines = ["xxxx"] * 5  # 5 lines of length 4
    result = bot._chunk_lines(lines, 10, "H")
    assert len(result) >= 2, f"setup must force >=2 chunks; got {result!r}"
    for chunk in result[1:]:
        assert not chunk.startswith("H"), (
            f"later chunks must NOT carry the header; got {chunk!r}"
        )


def test_chunk_lines_respects_max_len_within_budget() -> None:
    # AC1 mechanic: each chunk fits within `max_len` when no oversized line is present
    lines = ["aaaa", "bbbb", "cccc"]  # each len 4
    result = bot._chunk_lines(lines, 10, "H")
    for chunk in result:
        assert len(chunk) <= 10, (
            f"chunk must fit max_len=10; got len={len(chunk)} chunk={chunk!r}"
        )


def test_chunk_lines_never_splits_a_line_mid_line() -> None:
    # AC1 mechanic: lines are atomic — each input line appears verbatim somewhere
    lines = ["alpha", "beta", "gamma"]
    result = bot._chunk_lines(lines, 12, "H")
    joined = "\n".join(result)
    for line in lines:
        assert line in joined, (
            f"line {line!r} must appear verbatim (no mid-line split); got {result!r}"
        )


def test_chunk_lines_oversized_single_line_preserved_intact() -> None:
    # edge: a line whose own length exceeds max_len is emitted on its own chunk, intact
    long_line = "x" * 50  # len 50 > max_len 10
    result = bot._chunk_lines([long_line], 10, "H")
    assert long_line in result, (
        f"oversized line must be preserved as its own chunk, not truncated; got {result!r}"
    )


# === AC1 — pagination =======================================================


def test_cmd_list_paginates_when_body_exceeds_max_msg_len(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — body > MAX_MSG_LEN → multiple reply_text calls, every row present
    _patch_conn(monkeypatch, conn)
    # ~150 words: each row ~30 chars → ~4500 chars body, well past MAX_MSG_LEN=4000.
    for i in range(150):
        vocab.add_word(conn, CHAT, f"word{i:03d}")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    replies = _all_replies(update)
    assert len(replies) >= 2, (
        f"AC1: expected multiple reply_text calls for 150-word vocab; got {len(replies)}"
    )
    joined = "\n".join(replies)
    for i in range(150):
        assert f"• word{i:03d} (seen " in joined, (
            f"AC1: row for word{i:03d} must appear across the paginated output"
        )


def test_cmd_list_no_truncation_marker_for_large_vocab(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — pagination replaces the legacy "…" truncation; no marker may appear
    _patch_conn(monkeypatch, conn)
    for i in range(150):
        vocab.add_word(conn, CHAT, f"word{i:03d}")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    joined = "\n".join(_all_replies(update))
    assert "…" not in joined, (
        f"AC1: no '…' truncation marker may appear under pagination; got tail {joined[-200:]!r}"
    )


def test_cmd_list_single_message_for_small_vocab(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge: small vocab fits in one chunk → exactly one reply_text call
    _patch_conn(monkeypatch, conn)
    for w in ("alpha", "beta", "gamma", "delta", "epsilon"):
        vocab.add_word(conn, CHAT, w)

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    replies = _all_replies(update)
    assert len(replies) == 1, (
        f"small vocab must fit in a single message; got {len(replies)} replies"
    )


# === AC2 — per-row label suffix in BOTH branches ============================


def test_cmd_list_unfiltered_row_with_labels_has_alphabetical_suffix(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — unfiltered row appends ' — label1, label2' in alphabetical order
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    # Attach in deliberately non-alphabetical order to prove the join sorts.
    _attach(conn, CHAT, "horse", "type:animal")
    _attach(conn, CHAT, "horse", "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    line = next((ln for ln in reply.splitlines() if "horse" in ln), None)
    assert line is not None, f"AC2: horse row missing; got {reply!r}"
    assert line.endswith(" — pos:noun, type:animal"), (
        f"AC2: unfiltered row must end with ' — <alphabetical labels>'; got {line!r}"
    )


def test_cmd_list_unfiltered_row_without_labels_has_bare_format(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — words with zero labels keep bare '• <text> (seen N×, score S)'
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "lonely")  # no labels attached

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    line = next((ln for ln in reply.splitlines() if "lonely" in ln), None)
    assert line is not None, f"AC2: lonely row missing; got {reply!r}"
    assert " — " not in line, (
        f"AC2: rows for words with zero labels must NOT carry an em-dash label suffix; got {line!r}"
    )
    assert "• lonely (seen " in line and "score " in line, (
        f"AC2: bare row must keep '• <text> (seen N×, score S)' format; got {line!r}"
    )


def test_cmd_list_filtered_row_with_labels_has_alphabetical_suffix(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — filtered branch also carries the alphabetical label suffix
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "type:animal")
    _attach(conn, CHAT, "horse", "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun"])))

    reply = _last_reply(update)
    line = next((ln for ln in reply.splitlines() if "horse" in ln), None)
    assert line is not None, f"AC2: filtered horse row missing; got {reply!r}"
    assert line.endswith(" — pos:noun, type:animal"), (
        f"AC2: filtered row must end with alphabetical label list; got {line!r}"
    )


# === AC3 — --any OR-mode ====================================================


def test_cmd_list_any_flag_returns_union(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — '/list --any pos:noun type:medicine' returns the union
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    vocab.add_word(conn, CHAT, "unrelated")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")
    # 'unrelated' has neither label.

    update = _make_command_update()
    asyncio.run(
        bot.cmd_list(update, _make_context(["--any", "pos:noun", "type:medicine"]))
    )

    reply = "\n".join(_all_replies(update))
    assert "horse" in reply, f"AC3: 'horse' (pos:noun) must appear under OR; got {reply!r}"
    assert "pill" in reply, (
        f"AC3: 'pill' (type:medicine) must appear under OR; got {reply!r}"
    )
    assert "unrelated" not in reply, (
        f"AC3: 'unrelated' (neither label) must be excluded; got {reply!r}"
    )


def test_cmd_list_any_dedupes_words_with_both_labels(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — a word carrying both labels appears at most once under --any
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "aspirin")
    _attach(conn, CHAT, "aspirin", "pos:noun")
    _attach(conn, CHAT, "aspirin", "type:medicine")

    update = _make_command_update()
    asyncio.run(
        bot.cmd_list(update, _make_context(["--any", "pos:noun", "type:medicine"]))
    )

    reply = "\n".join(_all_replies(update))
    bullet_count = sum(1 for ln in reply.splitlines() if ln.startswith("• aspirin "))
    assert bullet_count == 1, (
        f"AC3: word with both labels must appear exactly once under --any; "
        f"got {bullet_count} rows in {reply!r}"
    )


def test_cmd_list_any_header_includes_flag(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — header reads 'Vocab (N) matching --any pos:noun type:medicine:'
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")

    update = _make_command_update()
    asyncio.run(
        bot.cmd_list(update, _make_context(["--any", "pos:noun", "type:medicine"]))
    )

    first = _all_replies(update)[0]
    assert first.startswith("Vocab (2) matching --any pos:noun type:medicine:"), (
        f"AC3: header must follow 'Vocab (N) matching --any <names>:' shape; "
        f"got {first.splitlines()[0]!r}"
    )


# === AC4 — --any position-locked at index 0 =================================


def test_cmd_list_any_at_non_zero_index_is_treated_as_bare_label(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — '--any' at index >= 1 parses as a regular label via parse_label_spec
    _patch_conn(monkeypatch, conn)
    # Two words both with pos:noun; neither has '--any' as a label.
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "dog")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "dog", "pos:noun")

    update = _make_command_update()
    # If '--any' were treated as the OR flag here, both words would appear (OR yields the
    # pos:noun set). AC4 locks the flag to index 0, so this is AND(pos:noun, --any) → empty.
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun", "--any"])))

    reply = _last_reply(update)
    assert reply == "no words match those labels", (
        f"AC4: '--any' at index>=1 must parse as a bare label, yielding zero AND-matches; "
        f"got {reply!r}"
    )


def test_cmd_list_any_recognised_case_insensitive_at_index_zero(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — args[0].strip().lower() == FOCUS_ANY_FLAG, so '--ANY' triggers OR
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")

    update = _make_command_update()
    asyncio.run(
        bot.cmd_list(update, _make_context(["--ANY", "pos:noun", "type:medicine"]))
    )

    reply = "\n".join(_all_replies(update))
    # OR-mode active: both words present.
    assert "horse" in reply and "pill" in reply, (
        f"AC4: '--ANY' must normalise via lower() and trigger OR-mode; got {reply!r}"
    )


def test_cmd_list_any_recognised_with_surrounding_whitespace_at_index_zero(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — '  --Any  ' must strip+lower to trigger OR-mode at index 0
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")

    update = _make_command_update()
    asyncio.run(
        bot.cmd_list(
            update, _make_context(["  --Any  ", "pos:noun", "type:medicine"])
        )
    )

    reply = "\n".join(_all_replies(update))
    assert "horse" in reply and "pill" in reply, (
        f"AC4: '  --Any  ' must strip+lower to trigger OR-mode; got {reply!r}"
    )


# === AC5 — AND default preserved ============================================


def test_cmd_list_without_any_keeps_and_semantics(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — '/list pos:noun type:medicine' (no --any) still requires both labels
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    vocab.add_word(conn, CHAT, "aspirin")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "pill", "type:medicine")
    _attach(conn, CHAT, "aspirin", "pos:noun")
    _attach(conn, CHAT, "aspirin", "type:medicine")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["pos:noun", "type:medicine"])))

    reply = "\n".join(_all_replies(update))
    assert "aspirin" in reply, (
        f"AC5: 'aspirin' has both labels; must appear under AND; got {reply!r}"
    )
    assert "horse" not in reply, (
        f"AC5: 'horse' missing type:medicine; AND must exclude it; got {reply!r}"
    )
    assert "pill" not in reply, (
        f"AC5: 'pill' missing pos:noun; AND must exclude it; got {reply!r}"
    )


# === AC6 — empty --any error path ===========================================


def test_cmd_list_any_alone_replies_exact_malformed_message(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — '/list --any' alone → exact '⚠️ malformed label spec: --any'
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")  # vocab non-empty to rule out empty-chat branch

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["--any"])))

    reply = _last_reply(update)
    assert reply == "⚠️ malformed label spec: --any", (
        f"AC6: exact wording '⚠️ malformed label spec: --any' required; got {reply!r}"
    )


def test_cmd_list_any_alone_makes_no_word_query(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 / error — short-circuits before any list_words / words_matching_labels call
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    def _boom(*args, **kwargs):
        raise AssertionError(
            "AC6: no DB read of words may happen for '/list --any'"
        )

    monkeypatch.setattr(vocab, "list_words", _boom)
    monkeypatch.setattr(vocab, "words_matching_labels", _boom)

    update = _make_command_update()
    # Must not raise — handler must short-circuit before either call.
    asyncio.run(bot.cmd_list(update, _make_context(["--any"])))

    reply = _last_reply(update)
    assert reply == "⚠️ malformed label spec: --any", (
        f"AC6: malformed --any reply must be exact even with DB readers boobytrapped; got {reply!r}"
    )


# === AC7 — existing messages preserved ======================================


def test_cmd_list_unfiltered_empty_chat_message_preserved(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — empty chat + no args keeps today's wording
    _patch_conn(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    assert reply == "Your vocab is empty. Add words with /add <word>.", (
        f"AC7: empty-chat unfiltered wording must be preserved exactly; got {reply!r}"
    )


def test_cmd_list_filtered_zero_matches_message_preserved(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — filtered + zero matches still reads 'no words match those labels'
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")  # lacks type:medicine

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context(["type:medicine"])))

    reply = _last_reply(update)
    assert reply == "no words match those labels", (
        f"AC7: filtered zero-match wording must be preserved; got {reply!r}"
    )


def test_cmd_list_malformed_spec_message_preserved_no_db_read(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 / error — malformed → '⚠️ malformed label spec: <bad>'; no DB read
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    def _boom(*args, **kwargs):
        raise AssertionError(
            "AC7/error: no DB read of words may happen for malformed spec"
        )

    monkeypatch.setattr(vocab, "list_words", _boom)
    monkeypatch.setattr(vocab, "words_matching_labels", _boom)

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([":foo"])))

    reply = _last_reply(update)
    assert reply.startswith("⚠️ "), (
        f"AC7/error: malformed-spec reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "malformed label spec" in reply, (
        f"AC7/error: reply must carry shared 'malformed label spec' phrasing; got {reply!r}"
    )
    assert ":foo" in reply, (
        f"AC7/error: offending token must be echoed; got {reply!r}"
    )


# === edges =================================================================


def test_cmd_list_any_dedupes_duplicate_tokens(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — '--any pos:noun pos:noun' dedupes via parse_label_spec
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "pill")
    _attach(conn, CHAT, "horse", "pos:noun")
    # 'pill' lacks pos:noun.

    update_dup = _make_command_update()
    asyncio.run(
        bot.cmd_list(update_dup, _make_context(["--any", "pos:noun", "pos:noun"]))
    )
    update_single = _make_command_update()
    asyncio.run(bot.cmd_list(update_single, _make_context(["--any", "pos:noun"])))

    reply_dup = "\n".join(_all_replies(update_dup))
    reply_single = "\n".join(_all_replies(update_single))

    assert "horse" in reply_dup and "horse" in reply_single, (
        f"horse must appear in both; got dup={reply_dup!r} single={reply_single!r}"
    )
    assert "pill" not in reply_dup and "pill" not in reply_single, (
        f"pill (no pos:noun) must appear in neither; got dup={reply_dup!r} single={reply_single!r}"
    )
    first_dup = _all_replies(update_dup)[0]
    assert "pos:noun pos:noun" not in first_dup, (
        f"deduped header must not repeat the spec token; got {first_dup!r}"
    )


def test_cmd_list_one_label_word_suffix_format(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — single-label suffix is ' — <label>' (no trailing comma)
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, CHAT, "horse", "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    line = next((ln for ln in reply.splitlines() if "horse" in ln), None)
    assert line is not None, f"horse row missing; got {reply!r}"
    assert line.endswith(" — pos:noun"), (
        f"single-label suffix must end with ' — pos:noun' (no trailing comma); got {line!r}"
    )


def test_cmd_list_many_labels_alphabetical_order(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — many labels joined alphabetically with ', '
    _patch_conn(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    # Attach in deliberately scrambled order; output must still be alphabetical.
    _attach(conn, CHAT, "horse", "type:animal")
    _attach(conn, CHAT, "horse", "pos:noun")
    _attach(conn, CHAT, "horse", "field:biology")

    update = _make_command_update()
    asyncio.run(bot.cmd_list(update, _make_context([])))

    reply = _last_reply(update)
    line = next((ln for ln in reply.splitlines() if "horse" in ln), None)
    assert line is not None, f"horse row missing; got {reply!r}"
    assert line.endswith(" — field:biology, pos:noun, type:animal"), (
        f"multi-label suffix must be alphabetical joined with ', '; got {line!r}"
    )
