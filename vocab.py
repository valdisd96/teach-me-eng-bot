"""Vocabulary CRUD and literal mention scanning.

Words are normalized to lowercased, stripped form on write and scanned
case-insensitively. Substring (literal) matching is intentional for this stage
— no lemmatization, no stemming. A mention bumps `mention_count` once per
scan call regardless of how many times the word literally appears.

FSRS state on words is left to a later change; this module only touches the
CRUD / mention-count columns.
"""

from __future__ import annotations

import datetime
import sqlite3


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize(text: str) -> str:
    return text.strip().lower()


def ensure_chat(conn: sqlite3.Connection, chat_id: int, tz: str = "UTC") -> None:
    """Insert a chat row with defaults if one doesn't already exist.

    The real /start config flow (added later) overwrites these defaults; this
    helper exists so vocab ops can create a chat row on demand and satisfy the
    words.chat_id foreign key.
    """
    conn.execute(
        "INSERT OR IGNORE INTO chats(chat_id, tz, created_at) VALUES (?, ?, ?)",
        (chat_id, tz, _now()),
    )


def add_word(conn: sqlite3.Connection, chat_id: int, text: str) -> bool:
    """Add a word/phrase. Returns True if newly inserted, False if a duplicate."""
    word = _normalize(text)
    if not word:
        raise ValueError("word must be non-empty")
    ensure_chat(conn, chat_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO words(chat_id, text, added_at) VALUES (?, ?, ?)",
        (chat_id, word, _now()),
    )
    return cur.rowcount > 0


def remove_word(conn: sqlite3.Connection, chat_id: int, text: str) -> bool:
    """Remove by exact normalized match. Returns True if a row was deleted."""
    word = _normalize(text)
    if not word:
        return False
    cur = conn.execute(
        "DELETE FROM words WHERE chat_id = ? AND text = ?",
        (chat_id, word),
    )
    return cur.rowcount > 0


def list_words(
    conn: sqlite3.Connection,
    chat_id: int,
    contains: str | None = None,
) -> list[sqlite3.Row]:
    """All words for a chat, optionally filtered by substring.

    Ordered by mention_count ASC, then added_at DESC — surfaces the least-
    mentioned and newest additions first, which is what `/list` wants.
    """
    if contains:
        needle = f"%{_normalize(contains)}%"
        return conn.execute(
            "SELECT * FROM words "
            "WHERE chat_id = ? AND text LIKE ? "
            "ORDER BY mention_count ASC, added_at DESC",
            (chat_id, needle),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM words WHERE chat_id = ? "
        "ORDER BY mention_count ASC, added_at DESC",
        (chat_id,),
    ).fetchall()


def scan_mentions(text: str, words: list[tuple[int, str]]) -> list[int]:
    """Return ids of vocab words that appear as literal substrings in `text`.

    Case-insensitive. Stored words are assumed already-normalized. Each word
    contributes to the result at most once regardless of occurrence count.
    """
    haystack = text.lower()
    return [wid for wid, w in words if w and w in haystack]


def bump_mentions(conn: sqlite3.Connection, word_ids: list[int]) -> None:
    """Increment mention_count and update last_used_at for the given word ids."""
    if not word_ids:
        return
    now = _now()
    conn.executemany(
        "UPDATE words "
        "SET mention_count = mention_count + 1, last_used_at = ? "
        "WHERE id = ?",
        [(now, wid) for wid in word_ids],
    )
