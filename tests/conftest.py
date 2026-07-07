"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

import db as db_mod


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh initialized SQLite connection backed by a tmp file."""
    c = db_mod.connect(tmp_path / "vocab.db")
    db_mod.init_db(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture(autouse=True)
def _clear_bot_state():
    """Reset bot.py's module-global state maps between tests.

    The in-progress gate checks every state map, so an entry leaked by one
    test would short-circuit unrelated handlers in the next.
    """
    yield
    mod = sys.modules.get("bot")
    if mod is None:
        return
    for name in (
        "games",
        "irregulars",
        "typed_drills",
        "cloze_sessions",
        "pending_game_filters",
        "sessions",
        "import_pending",
    ):
        getattr(mod, name).clear()
    mod.deferred_sessions.clear()
