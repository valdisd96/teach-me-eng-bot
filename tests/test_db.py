"""Schema-level tests for db.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db as db_mod


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_init_creates_expected_tables(conn: sqlite3.Connection) -> None:
    assert {"chats", "words", "push_log"}.issubset(_tables(conn))


def test_init_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "vocab.db"
    c = db_mod.connect(path)
    db_mod.init_db(c)
    db_mod.init_db(c)  # second call must not raise
    assert {"chats", "words", "push_log"}.issubset(_tables(c))
    c.close()


def test_words_unique_per_chat(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'ephemeral', '2026-04-21')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'ephemeral', '2026-04-21')"
        )


def test_same_word_allowed_across_chats(conn: sqlite3.Connection) -> None:
    for chat_id in (1, 2):
        conn.execute(
            "INSERT INTO chats(chat_id, tz, created_at) VALUES (?, 'UTC', '2026-04-21')",
            (chat_id,),
        )
        conn.execute(
            "INSERT INTO words(chat_id, text, added_at) VALUES (?, 'ephemeral', '2026-04-21')",
            (chat_id,),
        )
    count = conn.execute("SELECT COUNT(*) AS n FROM words").fetchone()["n"]
    assert count == 2


def test_cascade_delete_chat_removes_words_and_pushes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (7, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (7, 'placid', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO push_log(chat_id, sent_at, word_ids_json) VALUES (7, '2026-04-21', '[1]')"
    )
    conn.execute("DELETE FROM chats WHERE chat_id = 7")
    assert conn.execute("SELECT COUNT(*) AS n FROM words").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM push_log").fetchone()["n"] == 0


def test_words_default_fsrs_state_is_new(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'serendipity', '2026-04-21')"
    )
    row = conn.execute("SELECT state, stability, difficulty FROM words").fetchone()
    assert row["state"] == 0
    assert row["stability"] is None
    assert row["difficulty"] is None
