"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
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
