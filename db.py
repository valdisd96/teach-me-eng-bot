"""SQLite persistence for vocab, chat settings, and push history.

Schema is created on first `init_db()` call and is idempotent. FSRS columns on
`words` are written as NULL until a word is rated; see `vocab.py` (added in a
later change) for FSRS integration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "vocab.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id        INTEGER PRIMARY KEY,
    tz             TEXT    NOT NULL,
    pushes_per_day INTEGER NOT NULL DEFAULT 3,
    active_start   TEXT    NOT NULL DEFAULT '09:00',
    active_end     TEXT    NOT NULL DEFAULT '21:00',
    tone           TEXT    NOT NULL DEFAULT 'mixed',
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS words (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    text           TEXT    NOT NULL,
    added_at       TEXT    NOT NULL,
    mention_count  INTEGER NOT NULL DEFAULT 0,
    last_used_at   TEXT,
    stability      REAL,
    difficulty     REAL,
    state          INTEGER NOT NULL DEFAULT 0,
    step           INTEGER,
    due            TEXT,
    reps           INTEGER NOT NULL DEFAULT 0,
    lapses         INTEGER NOT NULL DEFAULT 0,
    last_review    TEXT,
    UNIQUE(chat_id, text),
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_words_chat ON words(chat_id);

CREATE TABLE IF NOT EXISTS push_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    sent_at        TEXT    NOT NULL,
    tg_message_id  INTEGER,
    word_ids_json  TEXT    NOT NULL,
    rated          INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_push_log_chat ON push_log(chat_id);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with sensible pragmas for a single-writer bot."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist. Safe to call on every startup.

    Also applies small forward-only migrations for columns added after the
    initial schema — keeps existing on-disk dbs usable.
    """
    conn.executescript(SCHEMA)
    if "step" not in _column_names(conn, "words"):
        conn.execute("ALTER TABLE words ADD COLUMN step INTEGER")
