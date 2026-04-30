"""Tests for /help discoverability — HELP_TEXT and COMMANDS surfaces."""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot


def test_help_getting_started_mentions_import_and_export() -> None:
    """The Getting started prose must point new users at bulk CSV flow.

    Issue #51: the COMMANDS list was updated in #50 but the narrative still
    only mentioned /add, leaving /import undiscoverable in the bot UI.
    """
    intro, _, _ = bot.HELP_TEXT.partition("*Commands*")
    assert "/import" in intro
    assert "/export" in intro


def test_help_text_lists_every_command() -> None:
    """Every entry in COMMANDS must appear in HELP_TEXT's Commands block."""
    _, _, commands_block = bot.HELP_TEXT.partition("*Commands*")
    for name, desc in bot.COMMANDS:
        assert f"/{name} — {desc}" in commands_block


def test_commands_includes_import_and_export() -> None:
    names = {name for name, _ in bot.COMMANDS}
    assert {"import", "export"}.issubset(names)
