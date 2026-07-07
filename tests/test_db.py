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


# --- words.translation column (issue #63) -----------------------------------


def _word_columns(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("PRAGMA table_info(words)").fetchall()
    return {r["name"]: {"type": r["type"], "notnull": r["notnull"]} for r in rows}


def test_words_has_translation_column_after_init(conn: sqlite3.Connection) -> None:  # AC1 — translation column exists, TEXT, NULL allowed
    cols = _word_columns(conn)
    assert "translation" in cols, f"expected translation column, got {sorted(cols)}"
    assert cols["translation"]["type"].upper() == "TEXT", (
        f"expected TEXT, got {cols['translation']['type']!r}"
    )
    assert cols["translation"]["notnull"] == 0, "translation must allow NULL"


def test_words_translation_default_is_null(conn: sqlite3.Connection) -> None:  # AC1 — fresh insert leaves translation NULL
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'placid', '2026-04-21')"
    )
    row = conn.execute("SELECT translation FROM words").fetchone()
    assert row["translation"] is None


def test_init_db_translation_migration_idempotent(tmp_path: Path) -> None:  # AC1 — second init_db must not raise
    path = tmp_path / "vocab.db"
    c = db_mod.connect(path)
    db_mod.init_db(c)
    db_mod.init_db(c)
    cols = _word_columns(c)
    assert "translation" in cols
    c.close()


# --- labels + word_labels schema (issue #82) -------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {
        r["name"]: {"type": r["type"], "notnull": r["notnull"], "pk": r["pk"]}
        for r in rows
    }


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return [
        {
            "table": r["table"],
            "from": r["from"],
            "to": r["to"],
            "on_delete": r["on_delete"],
        }
        for r in rows
    ]


def test_init_creates_labels_table(conn: sqlite3.Connection) -> None:  # AC1 — labels(id, chat_id, name) + UNIQUE + FK CASCADE
    assert "labels" in _tables(conn), f"expected labels table, got {sorted(_tables(conn))}"

    cols = _columns(conn, "labels")
    assert "id" in cols and cols["id"]["pk"] == 1, f"id must be PK; got {cols.get('id')!r}"
    assert "chat_id" in cols and cols["chat_id"]["notnull"] == 1, (
        f"chat_id must be NOT NULL; got {cols.get('chat_id')!r}"
    )
    assert "name" in cols and cols["name"]["notnull"] == 1, (
        f"name must be NOT NULL; got {cols.get('name')!r}"
    )

    # UNIQUE(chat_id, name): inserting the same (chat_id, name) twice must fail.
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute("INSERT INTO labels(chat_id, name) VALUES (1, 'pos:noun')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO labels(chat_id, name) VALUES (1, 'pos:noun')")

    fks = _foreign_keys(conn, "labels")
    chat_fk = next((fk for fk in fks if fk["from"] == "chat_id"), None)
    assert chat_fk is not None, f"labels.chat_id must FK; got {fks!r}"
    assert chat_fk["table"] == "chats", f"FK target must be chats, got {chat_fk!r}"
    assert chat_fk["on_delete"].upper() == "CASCADE", (
        f"labels.chat_id FK must ON DELETE CASCADE; got {chat_fk!r}"
    )


def test_init_creates_word_labels_table(conn: sqlite3.Connection) -> None:  # AC2 — word_labels(word_id, label_id) composite PK, FKs CASCADE
    assert "word_labels" in _tables(conn), (
        f"expected word_labels table, got {sorted(_tables(conn))}"
    )

    cols = _columns(conn, "word_labels")
    assert "word_id" in cols and cols["word_id"]["pk"] >= 1, (
        f"word_id must be part of PK; got {cols.get('word_id')!r}"
    )
    assert "label_id" in cols and cols["label_id"]["pk"] >= 1, (
        f"label_id must be part of PK; got {cols.get('label_id')!r}"
    )

    # Composite PK: same (word_id, label_id) twice must fail.
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'apple', '2026-04-21')"
    )
    conn.execute("INSERT INTO labels(chat_id, name) VALUES (1, 'pos:noun')")
    wid = conn.execute("SELECT id FROM words").fetchone()["id"]
    lid = conn.execute("SELECT id FROM labels").fetchone()["id"]
    conn.execute(
        "INSERT INTO word_labels(word_id, label_id) VALUES (?, ?)", (wid, lid)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO word_labels(word_id, label_id) VALUES (?, ?)", (wid, lid)
        )

    fks = _foreign_keys(conn, "word_labels")
    word_fk = next((fk for fk in fks if fk["from"] == "word_id"), None)
    label_fk = next((fk for fk in fks if fk["from"] == "label_id"), None)
    assert word_fk is not None and word_fk["table"] == "words", (
        f"word_id must FK to words; got {fks!r}"
    )
    assert word_fk["on_delete"].upper() == "CASCADE", (
        f"word_id FK must ON DELETE CASCADE; got {word_fk!r}"
    )
    assert label_fk is not None and label_fk["table"] == "labels", (
        f"label_id must FK to labels; got {fks!r}"
    )
    assert label_fk["on_delete"].upper() == "CASCADE", (
        f"label_id FK must ON DELETE CASCADE; got {label_fk!r}"
    )


def test_init_db_label_tables_idempotent(tmp_path: Path) -> None:  # AC3 — forward-only + idempotent: rows untouched, tables present
    path = tmp_path / "vocab.db"
    c = db_mod.connect(path)
    db_mod.init_db(c)

    # Seed the new tables so we can detect any data wipe across re-init.
    c.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    c.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'apple', '2026-04-21')"
    )
    c.execute("INSERT INTO labels(chat_id, name) VALUES (1, 'pos:noun')")
    wid = c.execute("SELECT id FROM words").fetchone()["id"]
    lid = c.execute("SELECT id FROM labels").fetchone()["id"]
    c.execute(
        "INSERT INTO word_labels(word_id, label_id) VALUES (?, ?)", (wid, lid)
    )

    db_mod.init_db(c)  # second call must not raise or wipe data

    assert {"labels", "word_labels"}.issubset(_tables(c))
    assert c.execute("SELECT COUNT(*) AS n FROM labels").fetchone()["n"] == 1, (
        "existing labels rows must survive re-init"
    )
    assert c.execute("SELECT COUNT(*) AS n FROM word_labels").fetchone()["n"] == 1, (
        "existing word_labels rows must survive re-init"
    )
    c.close()


def test_delete_word_cascades_to_word_labels(conn: sqlite3.Connection) -> None:  # AC6 — DELETE FROM words wipes word_labels rows
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'apple', '2026-04-21')"
    )
    conn.execute("INSERT INTO labels(chat_id, name) VALUES (1, 'pos:noun')")
    wid = conn.execute("SELECT id FROM words").fetchone()["id"]
    lid = conn.execute("SELECT id FROM labels").fetchone()["id"]
    conn.execute(
        "INSERT INTO word_labels(word_id, label_id) VALUES (?, ?)", (wid, lid)
    )

    conn.execute("DELETE FROM words WHERE id = ?", (wid,))

    assert conn.execute("SELECT COUNT(*) AS n FROM word_labels").fetchone()["n"] == 0, (
        "word_labels rows must cascade-delete with their word"
    )
    # The label row itself should remain — only word_labels cascades from words.
    assert conn.execute("SELECT COUNT(*) AS n FROM labels").fetchone()["n"] == 1, (
        "deleting a word must NOT delete labels"
    )


def test_delete_chat_cascades_to_labels_and_word_labels(conn: sqlite3.Connection) -> None:  # AC6 — DELETE FROM chats wipes labels + word_labels transitively
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (7, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (7, 'apple', '2026-04-21')"
    )
    conn.execute("INSERT INTO labels(chat_id, name) VALUES (7, 'pos:noun')")
    wid = conn.execute("SELECT id FROM words").fetchone()["id"]
    lid = conn.execute("SELECT id FROM labels").fetchone()["id"]
    conn.execute(
        "INSERT INTO word_labels(word_id, label_id) VALUES (?, ?)", (wid, lid)
    )

    conn.execute("DELETE FROM chats WHERE chat_id = 7")

    assert conn.execute("SELECT COUNT(*) AS n FROM labels").fetchone()["n"] == 0, (
        "labels must cascade-delete when their chat is removed"
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM word_labels").fetchone()["n"] == 0, (
        "word_labels must cascade transitively (via words and labels)"
    )


# --- words.remembered_streak column (issue #125) ----------------------------


def test_words_remembered_streak_default_is_zero(conn: sqlite3.Connection) -> None:  # AC7 — fresh insert leaves remembered_streak == 0.0
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    conn.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'fresh', '2026-04-21')"
    )
    row = conn.execute("SELECT remembered_streak FROM words").fetchone()
    assert row["remembered_streak"] == 0.0, (
        f"newly-inserted word must default to remembered_streak=0.0; got {row['remembered_streak']!r}"
    )

    cols = _word_columns(conn)
    assert "remembered_streak" in cols, (
        f"expected remembered_streak column, got {sorted(cols)}"
    )
    assert cols["remembered_streak"]["type"].upper() == "REAL", (
        f"remembered_streak must be REAL; got {cols['remembered_streak']['type']!r}"
    )
    assert cols["remembered_streak"]["notnull"] == 1, (
        "remembered_streak must be NOT NULL"
    )


def test_init_db_adds_remembered_streak_to_legacy_db(tmp_path: Path) -> None:  # AC8 — pre-existing on-disk db gets the column with value 0.0 on existing rows
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path, isolation_level=None)
    legacy.row_factory = sqlite3.Row
    # Hand-build a `words` row that predates the remembered_streak column.
    # Mirrors the shape of the schema *before* issue #125's column was added.
    legacy.execute(
        """
        CREATE TABLE chats (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    legacy.execute(
        """
        CREATE TABLE words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(chat_id, text),
            FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
        )
        """
    )
    legacy.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-04-21')"
    )
    legacy.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'legacy', '2026-04-21')"
    )
    legacy.close()

    # Re-open via the production connect+init path; the migration must add the
    # column without raising and pre-existing rows must materialise as 0.0.
    c = db_mod.connect(path)
    db_mod.init_db(c)
    cols = _word_columns(c)
    assert "remembered_streak" in cols, (
        f"migration must add remembered_streak column to legacy db; got {sorted(cols)}"
    )
    row = c.execute(
        "SELECT remembered_streak FROM words WHERE text = 'legacy'"
    ).fetchone()
    assert row is not None, "pre-existing row must survive the migration"
    assert row["remembered_streak"] == 0.0, (
        f"pre-existing rows must initialise to 0.0; got {row['remembered_streak']!r}"
    )

    # Re-running init_db must remain idempotent on a now-migrated db.
    db_mod.init_db(c)
    c.close()


# --- chats.pushes_per_day default + floor lift -------------------------------


def test_chats_pushes_per_day_default_is_four(conn: sqlite3.Connection) -> None:
    # Fresh schema: a chat row inserted without the column materialises as 4 —
    # the words-per-story validator floor (config_flow.MIN_WORDS).
    conn.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-07-07')"
    )
    row = conn.execute("SELECT pushes_per_day FROM chats").fetchone()
    assert row["pushes_per_day"] == 4, (
        f"chats.pushes_per_day must DEFAULT to 4; got {row['pushes_per_day']!r}"
    )


def test_init_db_lifts_pushes_per_day_below_four(conn: sqlite3.Connection) -> None:
    # Rows created under the old DEFAULT 3 (or hand-set lower) would fail the
    # 4–10 words-per-story validation on re-save; init_db lifts them to 4
    # while leaving already-valid values untouched.
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, created_at) "
        "VALUES (1, 'UTC', 3, '2026-07-07')"
    )
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, created_at) "
        "VALUES (2, 'UTC', 1, '2026-07-07')"
    )
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, created_at) "
        "VALUES (3, 'UTC', 4, '2026-07-07')"
    )
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, created_at) "
        "VALUES (4, 'UTC', 9, '2026-07-07')"
    )

    db_mod.init_db(conn)  # startup migration pass

    values = {
        r["chat_id"]: r["pushes_per_day"]
        for r in conn.execute("SELECT chat_id, pushes_per_day FROM chats").fetchall()
    }
    assert values[1] == 4, f"3 must be lifted to the floor of 4; got {values[1]!r}"
    assert values[2] == 4, f"1 must be lifted to the floor of 4; got {values[2]!r}"
    assert values[3] == 4, f"4 is already valid and must stay; got {values[3]!r}"
    assert values[4] == 9, f"9 is already valid and must stay; got {values[4]!r}"
