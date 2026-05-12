"""Tests for issue #136 — `/top` focus-scoped learning-progress report.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Behaviour asserted here is derived from that spec, not from `cmd_top`'s body.

Public surface under test:
- `bot.cmd_top(update, context)` — new async handler.
- `bot.COMMANDS` — gains a `("top", ...)` row (AC9).

Mocking shape mirrors `tests/test_focus.py` / `tests/test_remembered_label.py` —
temp-DB `conn` fixture, Telegram Update/Context are MagicMock + AsyncMock at the
architectural seam, `bot.conn` patched to the temp DB.
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


CHAT = 1360


# -- helpers -----------------------------------------------------------------


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)


def _all_replies(reply_text_mock: MagicMock) -> list[str]:
    out: list[str] = []
    for call in reply_text_mock.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _last_reply(reply_text_mock: MagicMock) -> str:
    replies = _all_replies(reply_text_mock)
    assert replies, "expected at least one reply_text call, got none"
    return replies[-1]


def _full_reply(reply_text_mock: MagicMock) -> str:
    """Whole output joined across paginated reply_text calls."""
    return "\n".join(_all_replies(reply_text_mock))


def _word_id(conn: sqlite3.Connection, text: str, chat: int = CHAT) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat, text)
    ).fetchone()["id"]


def _attach(conn: sqlite3.Connection, word: str, label: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, chat, label))


def _set_streak(conn: sqlite3.Connection, word: str, value: float, chat: int = CHAT) -> None:
    conn.execute(
        "UPDATE words SET remembered_streak = ? WHERE id = ?",
        (value, _word_id(conn, word, chat)),
    )


def _section_block(reply: str, header_prefix: str) -> list[str]:
    """Return lines inside `reply` belonging to the section whose header starts
    with `header_prefix` (e.g. 'Top ('). Stops at the next blank line."""
    lines = reply.splitlines()
    out: list[str] = []
    started = False
    for ln in lines:
        if not started:
            if ln.startswith(header_prefix):
                started = True
            continue
        if ln == "":
            break
        out.append(ln)
    return out


# === AC1 — no focus set =====================================================


def test_cmd_top_no_focus_set_replies_exact_message(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — exact "no focus set — set one with /focus first"
    _patch_bot(monkeypatch, conn)
    vocab.ensure_chat(conn, CHAT)  # focus_spec stays NULL

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _last_reply(update.message.reply_text)
    assert reply == "no focus set — set one with /focus first", (
        f"AC1 — must reply exactly 'no focus set — set one with /focus first'; "
        f"got {reply!r}"
    )


def test_cmd_top_no_focus_set_emits_only_one_message(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — and no other messages
    _patch_bot(monkeypatch, conn)
    # Seed some vocab so that an accidental fall-through would emit sections.
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    replies = _all_replies(update.message.reply_text)
    assert len(replies) == 1, (
        f"AC1 — no-focus branch must emit exactly one message; got {len(replies)}"
    )


# === AC2 — three sections in order, headers + blank-line separators =========


def test_cmd_top_three_sections_in_order_when_focus_set(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — Top, Forgotten, Remembered headers in that order
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, "horse", "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    pos_top = reply.find("Top (")
    pos_forgotten = reply.find("Forgotten (")
    pos_remembered = reply.find("Remembered (")
    assert pos_top != -1, f"AC2 — 'Top (' header must appear; got {reply!r}"
    assert pos_forgotten != -1, f"AC2 — 'Forgotten (' header must appear; got {reply!r}"
    assert pos_remembered != -1, f"AC2 — 'Remembered (' header must appear; got {reply!r}"
    assert pos_top < pos_forgotten < pos_remembered, (
        f"AC2 — section order must be Top → Forgotten → Remembered; "
        f"got positions top={pos_top}, forgotten={pos_forgotten}, "
        f"remembered={pos_remembered} in {reply!r}"
    )


def test_cmd_top_sections_separated_by_blank_line(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — blank line between adjacent sections
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, "horse", "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    # Pair each header with the index of the preceding line; that line must be "".
    lines = reply.splitlines()
    header_indices = [
        i for i, ln in enumerate(lines)
        if ln.startswith("Forgotten (") or ln.startswith("Remembered (")
    ]
    assert len(header_indices) >= 2, (
        f"AC2 — both Forgotten and Remembered headers must be present; got {reply!r}"
    )
    for idx in header_indices:
        assert idx >= 1 and lines[idx - 1] == "", (
            f"AC2 — header on line {idx} ({lines[idx]!r}) must be preceded by "
            f"a blank line; got prev={lines[idx - 1]!r}"
        )


def test_cmd_top_empty_section_shows_count_zero_and_none_line(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 + AC6 — empty section → count 0 and a single '  (none)' line
    _patch_bot(monkeypatch, conn)
    # Single word; carries focus label and is plain (not focus:hard, not remembered).
    # → Top has 1 row, Forgotten and Remembered are empty.
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, "horse", "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    assert "Forgotten (0):" in reply, (
        f"AC2 — empty Forgotten section must show count 0; got {reply!r}"
    )
    assert "Remembered (0):" in reply, (
        f"AC2 — empty Remembered section must show count 0; got {reply!r}"
    )
    forgotten_rows = _section_block(reply, "Forgotten (")
    remembered_rows = _section_block(reply, "Remembered (")
    assert forgotten_rows == ["  (none)"], (
        f"AC2 — empty Forgotten body must be a single '  (none)' line; "
        f"got {forgotten_rows!r}"
    )
    assert remembered_rows == ["  (none)"], (
        f"AC2 — empty Remembered body must be a single '  (none)' line; "
        f"got {remembered_rows!r}"
    )


# === AC3 — Top section content + ordering + format ==========================


def test_cmd_top_top_section_excludes_remembered_and_focus_hard(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — Top excludes both `remembered` and `focus:hard` rows
    _patch_bot(monkeypatch, conn)
    for w in ("alpha", "beta", "gamma", "delta"):
        vocab.add_word(conn, CHAT, w)
        _attach(conn, w, "pos:noun")
    _attach(conn, "beta", vocab.REMEMBERED_LABEL)
    _attach(conn, "gamma", vocab.FOCUS_HARD_LABEL)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    top_rows = _section_block(reply, "Top (")
    joined = "\n".join(top_rows)
    # alpha + delta are the only plain rows.
    assert any("alpha" in r for r in top_rows), (
        f"AC3 — 'alpha' (plain) must appear in Top; got {top_rows!r}"
    )
    assert any("delta" in r for r in top_rows), (
        f"AC3 — 'delta' (plain) must appear in Top; got {top_rows!r}"
    )
    assert "beta" not in joined, (
        f"AC3 — 'beta' (remembered) must NOT appear in Top; got {top_rows!r}"
    )
    assert "gamma" not in joined, (
        f"AC3 — 'gamma' (focus:hard) must NOT appear in Top; got {top_rows!r}"
    )


def test_cmd_top_top_section_sorted_by_streak_desc_then_text_asc(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — order: remembered_streak DESC, then text ASC
    _patch_bot(monkeypatch, conn)
    # apple=0.0, banana=2.0, cherry=1.5, durian=2.0 (tie with banana on streak)
    for w in ("apple", "banana", "cherry", "durian"):
        vocab.add_word(conn, CHAT, w)
        _attach(conn, w, "pos:noun")
    _set_streak(conn, "apple", 0.0)
    _set_streak(conn, "banana", 2.0)
    _set_streak(conn, "cherry", 1.5)
    _set_streak(conn, "durian", 2.0)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    top_rows = _section_block(reply, "Top (")
    positions = {
        w: next(i for i, r in enumerate(top_rows) if w in r)
        for w in ("apple", "banana", "cherry", "durian")
    }
    # 2.0-banana and 2.0-durian first, tie broken by text ASC → banana, durian
    # then cherry (1.5), then apple (0.0).
    assert positions["banana"] < positions["durian"], (
        f"AC3 — streak tie at 2.0 broken by text ASC: banana before durian; "
        f"got positions={positions}"
    )
    assert positions["durian"] < positions["cherry"], (
        f"AC3 — streak DESC: 2.0 (durian) before 1.5 (cherry); "
        f"got positions={positions}"
    )
    assert positions["cherry"] < positions["apple"], (
        f"AC3 — streak DESC: 1.5 (cherry) before 0.0 (apple); "
        f"got positions={positions}"
    )


def test_cmd_top_top_row_format_bullet_word_dash_score_1dp(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC3 — row format '• <word> — score <s>' with 1-decimal score
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "banana")
    vocab.add_word(conn, CHAT, "cherry")
    _attach(conn, "apple", "pos:noun")
    _attach(conn, "banana", "pos:noun")
    _attach(conn, "cherry", "pos:noun")
    _set_streak(conn, "apple", 0.0)
    _set_streak(conn, "banana", 2.0)
    _set_streak(conn, "cherry", 1.5)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    assert "• apple — score 0.0" in reply, (
        f"AC3 — apple row must be '• apple — score 0.0'; got {reply!r}"
    )
    assert "• banana — score 2.0" in reply, (
        f"AC3 — banana row must be '• banana — score 2.0'; got {reply!r}"
    )
    assert "• cherry — score 1.5" in reply, (
        f"AC3 — cherry row must be '• cherry — score 1.5' (one decimal); "
        f"got {reply!r}"
    )


# === AC4 — Forgotten section ================================================


def test_cmd_top_forgotten_section_focus_hard_sorted_text_asc(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — focus:hard words, text ASC, '• <word>' (no score)
    _patch_bot(monkeypatch, conn)
    # All in focus; gamma + alpha carry focus:hard, alphabetical order alpha, gamma.
    for w in ("alpha", "beta", "gamma"):
        vocab.add_word(conn, CHAT, w)
        _attach(conn, w, "pos:noun")
    _attach(conn, "gamma", vocab.FOCUS_HARD_LABEL)
    _attach(conn, "alpha", vocab.FOCUS_HARD_LABEL)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    forgotten_rows = _section_block(reply, "Forgotten (")
    assert "Forgotten (2):" in reply, (
        f"AC4 — Forgotten header must show count 2; got {reply!r}"
    )
    assert forgotten_rows == ["• alpha", "• gamma"], (
        f"AC4 — Forgotten section must be text-ASC '• <word>' rows with no score; "
        f"got {forgotten_rows!r}"
    )


# === AC5 — Remembered section ===============================================


def test_cmd_top_remembered_section_remembered_sorted_text_asc(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — remembered words, text ASC, '• <word>' (no score)
    _patch_bot(monkeypatch, conn)
    for w in ("zeta", "alpha", "mango"):
        vocab.add_word(conn, CHAT, w)
        _attach(conn, w, "pos:noun")
    _attach(conn, "zeta", vocab.REMEMBERED_LABEL)
    _attach(conn, "alpha", vocab.REMEMBERED_LABEL)
    # mango stays plain → in Top, not Remembered.
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    remembered_rows = _section_block(reply, "Remembered (")
    assert "Remembered (2):" in reply, (
        f"AC5 — Remembered header must show count 2; got {reply!r}"
    )
    assert remembered_rows == ["• alpha", "• zeta"], (
        f"AC5 — Remembered section must be text-ASC '• <word>' rows with no score; "
        f"got {remembered_rows!r}"
    )


# === AC6 — focus matches zero words → three-section form ====================


def test_cmd_top_zero_matches_uses_three_section_header_form(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC6 — zero matches → three-section header form, NOT 'no words match'
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")  # has no labels at all
    # Create label so the spec parses but matches nothing.
    vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    assert "no words match those labels" not in reply, (
        f"AC6 — zero matches must NOT reuse the /list 'no words match those labels' "
        f"message; got {reply!r}"
    )
    assert "Top (0):" in reply, f"AC6 — Top (0): expected; got {reply!r}"
    assert "Forgotten (0):" in reply, f"AC6 — Forgotten (0): expected; got {reply!r}"
    assert "Remembered (0):" in reply, f"AC6 — Remembered (0): expected; got {reply!r}"
    # And each empty section's body must be "(none)".
    for header in ("Top (", "Forgotten (", "Remembered ("):
        rows = _section_block(reply, header)
        assert rows == ["  (none)"], (
            f"AC6 — empty {header}…) section body must be '  (none)'; got {rows!r}"
        )


# === AC7 — --any flag honoured as OR-mode ===================================


def test_cmd_top_honours_any_or_mode(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — '--any' prefix → OR semantics across tokens
    _patch_bot(monkeypatch, conn)
    # apple has pos:noun only; banana has type:fruit only; neither has both.
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "banana")
    _attach(conn, "apple", "pos:noun")
    _attach(conn, "banana", "type:fruit")
    # OR-mode should bring in both.
    vocab.set_focus_spec(conn, CHAT, "--any pos:noun type:fruit")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    top_rows = _section_block(reply, "Top (")
    top_text = "\n".join(top_rows)
    assert "apple" in top_text, (
        f"AC7 — OR-mode '--any pos:noun type:fruit' must include apple; "
        f"got top rows {top_rows!r}"
    )
    assert "banana" in top_text, (
        f"AC7 — OR-mode '--any pos:noun type:fruit' must include banana; "
        f"got top rows {top_rows!r}"
    )


def test_cmd_top_default_uses_and_mode(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — no '--any' flag → AND across tokens
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "banana")
    _attach(conn, "apple", "pos:noun")  # only pos:noun
    _attach(conn, "banana", "pos:noun")
    _attach(conn, "banana", "type:fruit")  # both
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:fruit")  # AND

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    top_rows = _section_block(reply, "Top (")
    top_text = "\n".join(top_rows)
    assert "banana" in top_text, (
        f"AC7 — AND-mode must include banana (has both labels); got {top_rows!r}"
    )
    assert "apple" not in top_text, (
        f"AC7 — AND-mode must exclude apple (only one label); got {top_rows!r}"
    )


# === AC8 — pagination via _chunk_lines ======================================


def test_cmd_top_paginates_long_output_via_chunk_lines(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC8 — long output paginates; every row present; no truncation marker
    _patch_bot(monkeypatch, conn)
    # 400 rows * ~22 chars per row well exceeds Telegram's MAX_MSG_LEN=4000.
    n = 400
    for i in range(n):
        w = f"word{i:03d}"
        vocab.add_word(conn, CHAT, w)
        _attach(conn, w, "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    replies = _all_replies(update.message.reply_text)
    assert len(replies) >= 2, (
        f"AC8 — {n} rows must paginate across multiple messages "
        f"(otherwise truncation would have to occur); got {len(replies)}"
    )
    joined = "\n".join(replies)
    for i in range(n):
        assert f"word{i:03d}" in joined, (
            f"AC8 — every paginated row must appear; word{i:03d} missing"
        )
    assert "…" not in joined, (
        f"AC8 — pagination must not truncate (no '…' marker); tail={joined[-200:]!r}"
    )


# === AC9 — COMMANDS registration ============================================


def test_cmd_top_registered_in_COMMANDS() -> None:  # AC9 — /help + set_my_commands sees it
    names = [name for name, _desc in bot.COMMANDS]
    assert "top" in names, (
        f"AC9 — bot.COMMANDS must contain a 'top' entry so /help and "
        f"set_my_commands list it; got names={names!r}"
    )


def test_cmd_top_handler_callable() -> None:  # AC9 — bot.cmd_top is a public callable
    assert hasattr(bot, "cmd_top"), "AC9 — bot.cmd_top symbol must exist"
    assert callable(bot.cmd_top), (
        f"AC9 — bot.cmd_top must be callable; got {type(bot.cmd_top).__name__}"
    )


# === edges ==================================================================


def test_cmd_top_empty_vocab_with_focus_set(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — empty vocab + focus set → three-section form, all 0/(none)
    _patch_bot(monkeypatch, conn)
    # No words added; just set a focus.
    vocab.ensure_chat(conn, CHAT)
    vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    assert "Top (0):" in reply, f"edge — Top (0): expected on empty vocab; got {reply!r}"
    assert "Forgotten (0):" in reply, f"edge — Forgotten (0): expected; got {reply!r}"
    assert "Remembered (0):" in reply, f"edge — Remembered (0): expected; got {reply!r}"


def test_cmd_top_word_with_remembered_and_focus_hard_to_remembered(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — defensive overlap: remembered + focus:hard → Remembered wins
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "weird")
    _attach(conn, "weird", "pos:noun")
    _attach(conn, "weird", vocab.REMEMBERED_LABEL)
    _attach(conn, "weird", vocab.FOCUS_HARD_LABEL)
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    forgotten_rows = _section_block(reply, "Forgotten (")
    remembered_rows = _section_block(reply, "Remembered (")
    top_rows = _section_block(reply, "Top (")
    forgotten_text = "\n".join(forgotten_rows)
    remembered_text = "\n".join(remembered_rows)
    top_text = "\n".join(top_rows)
    assert "weird" in remembered_text, (
        f"edge — overlap row must appear under Remembered; got {remembered_rows!r}"
    )
    assert "weird" not in forgotten_text, (
        f"edge — overlap row must NOT also appear under Forgotten; "
        f"got {forgotten_rows!r}"
    )
    assert "weird" not in top_text, (
        f"edge — overlap row must NOT appear under Top; got {top_rows!r}"
    )


def test_cmd_top_streak_3_0_without_remembered_label_stays_in_top(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — streak==3.0 but no `remembered` label → still in Top with score 3.0
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "stale")
    _attach(conn, "stale", "pos:noun")
    _set_streak(conn, "stale", 3.0)  # no REMEMBERED_LABEL attached
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    reply = _full_reply(update.message.reply_text)
    top_rows = _section_block(reply, "Top (")
    remembered_rows = _section_block(reply, "Remembered (")
    assert any("stale" in r for r in top_rows), (
        f"edge — classification must be by label, not by score; "
        f"streak==3.0 sans label belongs in Top; got top={top_rows!r}"
    )
    assert all("stale" not in r for r in remembered_rows), (
        f"edge — must NOT appear in Remembered without the label; "
        f"got remembered={remembered_rows!r}"
    )
    assert "• stale — score 3.0" in reply, (
        f"edge — row must show 'score 3.0' with one decimal; got {reply!r}"
    )


def test_cmd_top_extra_args_ignored(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge — /top <anything> behaves identically to /top
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, "horse", "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")

    update_bare = _make_command_update()
    asyncio.run(bot.cmd_top(update_bare, _make_context([])))
    update_extra = _make_command_update()
    asyncio.run(
        bot.cmd_top(update_extra, _make_context(["pos:noun", "foo", "bar"]))
    )

    reply_bare = _full_reply(update_bare.message.reply_text)
    reply_extra = _full_reply(update_extra.message.reply_text)
    assert reply_bare == reply_extra, (
        f"edge — extra args must not alter output; got bare={reply_bare!r}, "
        f"extra={reply_extra!r}"
    )


# === error condition — is_allowed ===========================================


def test_cmd_top_unauthorised_user_returns_silently(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # error — is_allowed False → handler returns with no reply_text
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _attach(conn, "horse", "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")
    monkeypatch.setattr(bot, "is_allowed", lambda update: False)

    update = _make_command_update()
    asyncio.run(bot.cmd_top(update, _make_context([])))

    assert _all_replies(update.message.reply_text) == [], (
        f"error — unauthorised user must yield zero reply_text calls; got "
        f"{_all_replies(update.message.reply_text)!r}"
    )
