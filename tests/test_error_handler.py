"""Tests for bot.on_error — the central PTB error handler.

Without a registered error handler an exception escaping any handler is
swallowed by PTB's fallback and the user gets dead silence. `on_error` logs
every error, notifies the chat when the update carries one, and must never
raise itself. Mocking shape mirrors tests/test_games_cancel.py: Update /
Context are MagicMock + AsyncMock at the architectural seam.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)


CHAT = 40100

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- helpers -----------------------------------------------------------------


def _make_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    return update


def _make_context(error: BaseException) -> MagicMock:
    ctx = MagicMock()
    ctx.error = error
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


# -- on_error ------------------------------------------------------------------


def test_on_error_notifies_chat_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    # An update with an effective_chat → the user gets the ⚠️ apology and the
    # original exception lands in the log with its traceback.
    update = _make_update()
    ctx = _make_context(RuntimeError("boom"))

    with caplog.at_level(logging.ERROR, logger="bot"):
        asyncio.run(bot.on_error(update, ctx))

    ctx.bot.send_message.assert_awaited_once()
    kwargs = ctx.bot.send_message.await_args.kwargs
    assert kwargs.get("chat_id") == CHAT, (
        f"the apology must target the failing chat; got {kwargs!r}"
    )
    assert kwargs.get("text", "").startswith("⚠️"), (
        f"the user-facing text must be the ⚠️ apology; got {kwargs.get('text')!r}"
    )
    assert "Unhandled exception in handler" in caplog.text
    assert any(
        r.exc_info and isinstance(r.exc_info[1], RuntimeError)
        for r in caplog.records
    ), "the original exception must be logged with exc_info"


def test_on_error_update_none_only_logs() -> None:
    # Polling-level network errors arrive with update=None — no chat to
    # notify, and no exception may escape.
    ctx = _make_context(RuntimeError("network sad"))

    asyncio.run(bot.on_error(None, ctx))  # must not raise

    ctx.bot.send_message.assert_not_awaited()


def test_on_error_send_failure_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Telegram refusing the apology (e.g. chat gone) must never raise out of
    # the error handler — that would loop PTB's error machinery.
    update = _make_update()
    ctx = _make_context(RuntimeError("boom"))
    ctx.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

    with caplog.at_level(logging.ERROR, logger="bot"):
        asyncio.run(bot.on_error(update, ctx))  # must not raise

    assert "Failed to notify chat" in caplog.text


def test_on_error_is_registered_in_bot_main() -> None:
    # Wiring check (docs-test style): the handler is only useful if bot.py
    # actually registers it on the Application.
    bot_py_text = (REPO_ROOT / "bot.py").read_text(encoding="utf-8")
    assert "app.add_error_handler(on_error)" in bot_py_text, (
        "bot.py must register on_error via app.add_error_handler(on_error)"
    )
