"""Tests for issue #101 — remove the LLM label-suggestion follow-up from /add.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. The
diff is pure deletion: `label_suggestor.py` is gone, `bot.py` no longer wires
`pending_labels`, `_send_label_suggestions`, `on_label_suggest`, or any
`CallbackQueryHandler` matching `^lbl:`. After `/add`, `cmd_add` sends
exactly one user-facing reply — `➕ Added: <normalized>` for a fresh word, or
`Already in your vocab: <normalized>` for a duplicate — with no follow-up.

Telegram Update/Context is mocked at the seam (AsyncMock + MagicMock) per
the project convention (see `tests/test_label_commands.py`).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import vocab  # noqa: E402


CHAT = 9101


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
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    """Wire bot.conn + neutralize translation seams.

    Translation-on-add still happens per spec, but the translator is not
    under test here — stub all entry points so no network IO occurs.
    """
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(
        bot.translator, "translate",
        lambda text, target, source="auto": "stub-translation",
    )
    if hasattr(bot.sched_module, "compose_translation"):
        monkeypatch.setattr(
            bot.sched_module, "compose_translation",
            AsyncMock(return_value="stub-translation"),
        )


def _reply_calls(update: MagicMock) -> list:
    return list(update.message.reply_text.call_args_list)


def _reply_texts(update: MagicMock) -> list[str]:
    out: list[str] = []
    for call in update.message.reply_text.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _all_callback_data(update: MagicMock) -> list[str]:
    """Every callback_data carried by any reply_text reply_markup."""
    out: list[str] = []
    for call in update.message.reply_text.call_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup is None:
            continue
        rows = getattr(markup, "inline_keyboard", None) or []
        for row in rows:
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                if cb:
                    out.append(cb)
    return out


# === AC1 — fresh /add: single ➕ Added reply, no follow-up ==================


def test_cmd_add_new_word_sends_single_added_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — exactly one reply prefixed "➕ Added:"; error: LLM-unavailability no longer affects /add
    _patch_bot(monkeypatch, conn)
    update = _make_command_update()
    ctx = _make_context(["coffee"])

    asyncio.run(bot.cmd_add(update, ctx))

    calls = _reply_calls(update)
    assert len(calls) == 1, (
        f"AC1: cmd_add must send exactly one reply on a fresh add (no suggester follow-up); "
        f"got {len(calls)} calls with texts {_reply_texts(update)!r}"
    )
    text = _reply_texts(update)[0]
    assert text.startswith("➕ Added:"), (
        f"AC1: fresh-add reply must start with '➕ Added:'; got {text!r}"
    )
    assert "coffee" in text, (
        f"AC1: reply must echo the normalized word 'coffee'; got {text!r}"
    )


def test_cmd_add_new_word_sends_no_lbl_callback_data(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC1 — no reply may carry lbl:* callback_data
    _patch_bot(monkeypatch, conn)
    update = _make_command_update()
    ctx = _make_context(["umbrella"])

    asyncio.run(bot.cmd_add(update, ctx))

    cbs = _all_callback_data(update)
    assert not any(c.startswith("lbl:") for c in cbs), (
        f"AC1: cmd_add must not surface any lbl:* callback_data after add; got {cbs!r}"
    )


# === AC2 — duplicate /add: single 'Already in your vocab' reply =============


def test_cmd_add_duplicate_replies_already_in_vocab_once(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2 — duplicate: one reply 'Already in your vocab:', no lbl:*
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()
    ctx = _make_context(["horse"])

    asyncio.run(bot.cmd_add(update, ctx))

    calls = _reply_calls(update)
    assert len(calls) == 1, (
        f"AC2: duplicate /add must send exactly one reply; got {len(calls)} "
        f"with texts {_reply_texts(update)!r}"
    )
    text = _reply_texts(update)[0]
    assert text.startswith("Already in your vocab:"), (
        f"AC2: duplicate reply must start with 'Already in your vocab:'; got {text!r}"
    )
    cbs = _all_callback_data(update)
    assert not any(c.startswith("lbl:") for c in cbs), (
        f"AC2: duplicate path must not surface lbl:* callback_data; got {cbs!r}"
    )


# === AC3 — bot module no longer exposes the suggestor mechanism =============


def test_bot_no_label_suggestor_import() -> None:  # AC3 — bot.label_suggestor must not exist
    assert not hasattr(bot, "label_suggestor"), (
        "AC3: bot.py must no longer import label_suggestor"
    )


def test_bot_no_pending_labels_attribute() -> None:  # AC3 — bot.pending_labels removed
    assert not hasattr(bot, "pending_labels"), (
        "AC3: bot.pending_labels registry must be removed"
    )


def test_bot_no_send_label_suggestions_helper() -> None:  # AC3 — bot._send_label_suggestions removed
    assert not hasattr(bot, "_send_label_suggestions"), (
        "AC3: bot._send_label_suggestions helper must be removed"
    )


def test_bot_no_on_label_suggest_callback() -> None:  # AC3 — bot.on_label_suggest removed
    assert not hasattr(bot, "on_label_suggest"), (
        "AC3: bot.on_label_suggest callback must be removed"
    )


def test_label_suggestor_module_unimportable() -> None:  # AC3 — `import label_suggestor` raises ModuleNotFoundError
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("label_suggestor")


# === AC4 — no ^lbl: CallbackQueryHandler registration =======================


def test_bot_main_registers_no_lbl_callback_handler() -> None:  # AC4 — no '^lbl:' pattern anywhere in bot.py
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert "lbl:" not in src, (
        "AC4: bot.py must not register any CallbackQueryHandler matching '^lbl:' — "
        "the suggestor mechanism is gone"
    )


# === AC5 — label_suggestor.py absent from the repo ==========================


def test_label_suggestor_file_absent_from_repo() -> None:  # AC5 — label_suggestor.py deleted
    repo_root = Path(bot.__file__).parent
    assert not (repo_root / "label_suggestor.py").exists(), (
        "AC5: label_suggestor.py must be deleted from the repo root"
    )


# === AC6 — existing label-management surfaces still present ================


def test_label_management_surfaces_still_callable() -> None:  # AC6 — invariant: /label, /unlabel, /labels still wired
    for name in ("cmd_label", "cmd_unlabel", "cmd_labels"):
        assert hasattr(bot, name), (
            f"AC6: bot.{name} must remain — this PR deletes the suggester only"
        )
        assert asyncio.iscoroutinefunction(getattr(bot, name)), (
            f"AC6: bot.{name} must still be an async handler"
        )


# === edge cases =============================================================


def test_cmd_add_with_no_args_sends_usage_hint(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge: empty /add — unchanged usage hint, no lbl:* buttons
    _patch_bot(monkeypatch, conn)
    update = _make_command_update()
    ctx = _make_context([])

    asyncio.run(bot.cmd_add(update, ctx))

    calls = _reply_calls(update)
    assert len(calls) >= 1, "edge: empty /add must reply with a usage hint"
    cbs = _all_callback_data(update)
    assert not any(c.startswith("lbl:") for c in cbs), (
        f"edge: empty /add must not produce label-suggester buttons; got {cbs!r}"
    )


def test_cmd_add_unicode_word_sends_single_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge: unicode word — vocab.add_word handles it; no suggestion follow-up
    _patch_bot(monkeypatch, conn)
    update = _make_command_update()
    ctx = _make_context(["café"])

    asyncio.run(bot.cmd_add(update, ctx))

    calls = _reply_calls(update)
    assert len(calls) == 1, (
        f"edge: unicode add must produce exactly one reply (no follow-up); "
        f"got {len(calls)}: {_reply_texts(update)!r}"
    )
    cbs = _all_callback_data(update)
    assert not any(c.startswith("lbl:") for c in cbs), (
        f"edge: unicode add must not surface lbl:* callbacks; got {cbs!r}"
    )


def test_cmd_add_propagates_value_error_as_warning_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge: vocab.add_word ValueError → ⚠️ reply, no suggestion follow-up
    _patch_bot(monkeypatch, conn)
    # Neutralize the /add spell-check so the fake word reaches add_word.
    monkeypatch.setattr(bot.spelling, "suggest", lambda w: None)

    def boom(*_args, **_kwargs):
        raise ValueError("nope")

    monkeypatch.setattr(bot.vocab, "add_word", boom)
    update = _make_command_update()
    ctx = _make_context(["badword"])

    asyncio.run(bot.cmd_add(update, ctx))

    texts = _reply_texts(update)
    assert texts, "edge: ValueError path must still produce a reply"
    assert any("⚠" in t for t in texts), (
        f"edge: vocab.add_word ValueError must surface as a ⚠️ reply; got {texts!r}"
    )
    cbs = _all_callback_data(update)
    assert not any(c.startswith("lbl:") for c in cbs), (
        f"edge: ValueError path must not surface lbl:* callbacks; got {cbs!r}"
    )
