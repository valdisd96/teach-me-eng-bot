"""Tests for issue #120 — `/status` split into System / Vocab / Model sections.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Behaviour asserted here is derived from the Behavioral spec block; nothing is
inferred from function bodies.

Mocking shape mirrors `tests/test_focus.py` — Telegram Update / Context are
MagicMock + AsyncMock at the architectural seam, `bot.conn` is patched to the
temp-DB fixture, and the async LLM helpers (`llm.health`, `llm.usage`) are
mocked so the assertions can pin section text without depending on backend
state.
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


CHAT = 12000


def _make_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.args = []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _patch_bot(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    *,
    server: str = "openrouter (model=stub)",
    usage: str = "$0.012 used / limit: unlimited, rate 200 req / 10s",
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot.llm, "health", AsyncMock(return_value=server))
    monkeypatch.setattr(bot.llm, "usage", AsyncMock(return_value=usage))


def _invoke_status(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    *,
    chat_id: int = CHAT,
    server: str = "openrouter (model=stub)",
    usage: str = "$0.012 used / limit: unlimited, rate 200 req / 10s",
) -> str:
    _patch_bot(monkeypatch, conn, server=server, usage=usage)
    update = _make_update(chat_id)
    asyncio.run(bot.cmd_status(update, _make_context()))
    assert update.message.reply_text.await_count == 1, (
        "cmd_status must reply exactly once; "
        f"awaited {update.message.reply_text.await_count} time(s)"
    )
    return update.message.reply_text.await_args.args[0]


def _section_lines(reply: str, header: str) -> list[str]:
    """Lines that belong to a section (header excluded), up to the next blank line."""
    lines = reply.splitlines()
    try:
        start = lines.index(header) + 1
    except ValueError:
        raise AssertionError(
            f"section header {header!r} not found in reply:\n{reply}"
        )
    out: list[str] = []
    for line in lines[start:]:
        if line.strip() == "":
            break
        out.append(line)
    return out


# === AC7 — three section headers in order ===================================


def test_status_has_three_section_headers_in_order(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC7 — System / Vocab / Model headers each on their own line, in order
    vocab.ensure_chat(conn, CHAT)
    reply = _invoke_status(monkeypatch, conn)
    lines = reply.splitlines()

    def _idx(header: str) -> int:
        assert header in lines, (
            f"AC7 — expected literal header line {header!r}; reply:\n{reply}"
        )
        return lines.index(header)

    i_sys, i_voc, i_mod = _idx("System"), _idx("Vocab"), _idx("Model")
    assert i_sys < i_voc < i_mod, (
        f"AC7 — section headers must appear in order System → Vocab → Model; "
        f"got indices System={i_sys}, Vocab={i_voc}, Model={i_mod}"
    )


# === AC8 — System section keeps existing rows and drops Vocab: ==============


def test_status_system_section_has_no_vocab_row(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC8 — System has Hardware/OS/Version/Load/Temp/Disk, NOT Vocab:
    vocab.ensure_chat(conn, CHAT)
    reply = _invoke_status(monkeypatch, conn)
    system_block = "\n".join(_section_lines(reply, "System"))

    for key in ("Hardware:", "OS:", "Version:", "Load:", "Temp:", "Disk"):
        assert key in system_block, (
            f"AC8 — System section must contain {key!r} row; got:\n{system_block}"
        )
    assert "Vocab:" not in system_block, (
        f"AC8 — System section must NOT carry the old 'Vocab:' row; got:\n{system_block}"
    )


# === AC9 — Vocab section: Words / Labels / Focus ============================


def test_status_vocab_section_has_three_rows(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC9 — Vocab section is exactly three rows: Words / Labels / Focus
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.set_focus_spec(conn, CHAT, "pos:noun")
    reply = _invoke_status(monkeypatch, conn)

    vocab_lines = _section_lines(reply, "Vocab")
    assert len(vocab_lines) == 3, (
        f"AC9 — Vocab section must contain exactly three rows; got "
        f"{len(vocab_lines)}:\n{vocab_lines}"
    )
    assert vocab_lines[0] == "  Words: 2", (
        f"AC9 — first Vocab row must be '  Words: <n>'; got {vocab_lines[0]!r}"
    )
    assert vocab_lines[1] == "  Labels: 1", (
        f"AC9 — second Vocab row must be '  Labels: <n>'; got {vocab_lines[1]!r}"
    )
    assert vocab_lines[2] == "  Focus: pos:noun", (
        f"AC9 — third Vocab row must be '  Focus: <spec-or-none>'; got "
        f"{vocab_lines[2]!r}"
    )


def test_status_focus_row_says_none_when_unset(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC9 — get_focus_spec() returns None → "  Focus: none"
    vocab.ensure_chat(conn, CHAT)
    assert vocab.get_focus_spec(conn, CHAT) is None, (
        "precondition: focus_spec must be NULL for this test"
    )
    reply = _invoke_status(monkeypatch, conn)
    vocab_lines = _section_lines(reply, "Vocab")
    assert "  Focus: none" in vocab_lines, (
        f"AC9 — unset focus must render as '  Focus: none'; got Vocab rows:\n"
        f"{vocab_lines}"
    )


def test_status_focus_row_echoes_stored_spec(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC9 — stored spec echoed verbatim
    vocab.set_focus_spec(conn, CHAT, "pos:noun type:medicine")
    reply = _invoke_status(monkeypatch, conn)
    vocab_lines = _section_lines(reply, "Vocab")
    assert "  Focus: pos:noun type:medicine" in vocab_lines, (
        f"AC9 — stored spec must be echoed verbatim; got Vocab rows:\n{vocab_lines}"
    )


def test_status_focus_row_echoes_any_prefix(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # edge — '--any' prefix preserved, NOT stripped
    vocab.set_focus_spec(conn, CHAT, "--any type:body type:medicine")
    reply = _invoke_status(monkeypatch, conn)
    vocab_lines = _section_lines(reply, "Vocab")
    assert "  Focus: --any type:body type:medicine" in vocab_lines, (
        f"edge — '--any' prefix must be preserved verbatim; got Vocab rows:\n"
        f"{vocab_lines}"
    )


def test_status_renders_zero_words_and_labels(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # edge — empty chat → "Words: 0", "Labels: 0", "Focus: none"
    vocab.ensure_chat(conn, CHAT)
    reply = _invoke_status(monkeypatch, conn)
    vocab_lines = _section_lines(reply, "Vocab")
    assert "  Words: 0" in vocab_lines, (
        f"edge — empty chat must show 'Words: 0'; got:\n{vocab_lines}"
    )
    assert "  Labels: 0" in vocab_lines, (
        f"edge — empty chat must show 'Labels: 0'; got:\n{vocab_lines}"
    )
    assert "  Focus: none" in vocab_lines, (
        f"edge — empty chat must show 'Focus: none'; got:\n{vocab_lines}"
    )


# === AC10 — Model section: Server + Usage only ==============================


def test_status_model_section_has_server_and_usage_rows(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC10 — Model has "  Server: <health>" and "  Usage: <usage>"
    vocab.ensure_chat(conn, CHAT)
    reply = _invoke_status(
        monkeypatch,
        conn,
        server="openrouter (model=stub)",
        usage="$0.012 used / limit: unlimited, rate 200 req / 10s",
    )
    model_lines = _section_lines(reply, "Model")
    assert "  Server: openrouter (model=stub)" in model_lines, (
        f"AC10 — Model section must carry '  Server: <health>'; got:\n{model_lines}"
    )
    assert (
        "  Usage: $0.012 used / limit: unlimited, rate 200 req / 10s"
        in model_lines
    ), (
        f"AC10 — Model section must carry '  Usage: <usage>'; got:\n{model_lines}"
    )


def test_status_model_section_omits_name_endpoint_bench(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # AC10 — Model section MUST NOT carry Name:/Endpoint:/Bench: rows
    vocab.ensure_chat(conn, CHAT)
    reply = _invoke_status(monkeypatch, conn)
    model_block = "\n".join(_section_lines(reply, "Model"))

    for forbidden in ("Name:", "Endpoint:", "Bench:"):
        assert forbidden not in model_block, (
            f"AC10 — Model section must NOT contain {forbidden!r}; got:\n{model_block}"
        )
