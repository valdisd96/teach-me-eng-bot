"""Tests for the new-word introduction quota.

Words with fewer than `vocab.INTRO_GRADUATION_REPS` FSRS ratings form the
introduction pool. `vocab.select_session_words` seeds up to
`cloze.MAX_INTRO_WORDS` of them into each daily cloze-story session while
bypassing the /focus label filter — so a freshly `/add`-ed unlabelled word
is guaranteed exposure even under an active focus. Covered here:

- `vocab.select_session_words` wiring: intro seeding + focus bypass
  (reps cutoff, remembered exclusion), focus scoping of the regular picks,
  fallback when the intro pool is empty, no duplicates, sub-`n` results
  on small pools.
- `scheduler.compose_session` marks intro words so the session message can
  badge them 🆕.
"""

from __future__ import annotations

import asyncio
import datetime
import random
import sqlite3

import cloze
import config_flow
import scheduler
import vocab


CHAT = 810
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _seed_chat(conn: sqlite3.Connection, tz: str = "UTC") -> None:
    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings(tz, 6, "09:00", "21:00", "funny", "ru"),
    )


def _word_id(conn: sqlite3.Connection, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (CHAT, text)
    ).fetchone()["id"]


def _set_reps(conn: sqlite3.Connection, text: str, reps: int) -> None:
    conn.execute(
        "UPDATE words SET reps = ? WHERE chat_id = ? AND text = ?",
        (reps, CHAT, text),
    )


# --- vocab.select_session_words -------------------------------------------------


def test_session_words_seed_intro_words_bypassing_focus(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")  # unlabelled, reps=0
    vocab.add_word(conn, CHAT, "labelled")
    _set_reps(conn, "labelled", vocab.INTRO_GRADUATION_REPS)
    label_id = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, "labelled"), label_id)

    rows = vocab.select_session_words(
        conn, CHAT, 5, names=["pos:noun"], rng=random.Random(0), now=NOW
    )
    texts = [r["text"] for r in rows]
    assert texts[0] == "fresh", (
        "the introduction seed must draw the unlabelled new word despite the "
        f"focus filter, and it comes first; got {texts!r}"
    )
    assert "labelled" in texts, "regular picks fill the remaining slots"


def test_session_words_regular_picks_respect_focus(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "labelled")
    vocab.add_word(conn, CHAT, "outside")
    _set_reps(conn, "labelled", vocab.INTRO_GRADUATION_REPS)
    _set_reps(conn, "outside", vocab.INTRO_GRADUATION_REPS)
    label_id = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, "labelled"), label_id)

    rows = vocab.select_session_words(
        conn, CHAT, 5, names=["pos:noun"], rng=random.Random(0), now=NOW
    )
    assert [r["text"] for r in rows] == ["labelled"], (
        "with an empty introduction pool, only focus-matching words may be "
        f"selected; got {[r['text'] for r in rows]!r}"
    )


def test_session_words_cap_intro_seeds(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    for w in ("alpha", "beta", "gamma", "delta"):
        vocab.add_word(conn, CHAT, w)  # all reps=0 → all intro-phase

    rows = vocab.select_session_words(
        conn, CHAT, 3, max_intro=2, rng=random.Random(0), now=NOW
    )
    # All four are also in the regular (unfiltered) pool, so the session still
    # fills up to n — but at most max_intro come from the intro-first draw.
    assert len(rows) == 3
    assert len({r["id"] for r in rows}) == 3, "no duplicate words in a session"


def test_session_words_fewer_than_n_on_small_pool(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "solo")
    rows = vocab.select_session_words(
        conn, CHAT, 8, rng=random.Random(0), now=NOW
    )
    assert [r["text"] for r in rows] == ["solo"]


def test_session_words_exclude_remembered(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")
    vocab.add_word(conn, CHAT, "known")
    _set_reps(conn, "known", vocab.INTRO_GRADUATION_REPS)
    label_id = vocab.get_or_create_label(conn, CHAT, vocab.REMEMBERED_LABEL)
    vocab.attach_label(conn, _word_id(conn, "known"), label_id)

    rows = vocab.select_session_words(
        conn, CHAT, 5, rng=random.Random(0), now=NOW
    )
    assert [r["text"] for r in rows] == ["fresh"]


def test_session_words_empty_vocab_returns_empty(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    assert vocab.select_session_words(conn, CHAT, 5, now=NOW) == []


# --- scheduler.compose_session intro badge ---------------------------------------


def test_compose_session_intro_flag_drives_new_word_section(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")  # reps=0 → intro
    vocab.add_word(conn, CHAT, "veteran")
    _set_reps(conn, "veteran", vocab.INTRO_GRADUATION_REPS)

    async def llm_ok(msgs):
        return "The fresh bread pleased the veteran baker."

    session = asyncio.run(
        scheduler.compose_session(
            conn, CHAT, llm_chat=llm_ok, n_words=5, rng=random.Random(0), now=NOW
        )
    )
    assert session is not None
    flags = {b.word: b.is_intro for b in session.blanks}
    assert flags == {"fresh": True, "veteran": False}
    body = cloze.format_session_message(session, rng=random.Random(0))
    assert "🆕" in body and "fresh" in body
