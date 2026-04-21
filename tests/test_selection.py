"""FSRS rating + selection-formula tests."""

from __future__ import annotations

import datetime
import random
import sqlite3

import pytest
from fsrs import Rating

import vocab


CHAT = 500
UTC = datetime.timezone.utc


def _wid(conn: sqlite3.Connection, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id=? AND text=?", (CHAT, text)
    ).fetchone()["id"]


def _set_added(conn: sqlite3.Connection, text: str, days_ago: float) -> None:
    ts = datetime.datetime.now(UTC) - datetime.timedelta(days=days_ago)
    conn.execute(
        "UPDATE words SET added_at = ? WHERE chat_id = ? AND text = ?",
        (ts.strftime(vocab._TS_FMT), CHAT, text),
    )


# --- rate_word ---------------------------------------------------------------


def test_rate_good_initializes_fsrs_state(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "ephemeral")
    wid = _wid(conn, "ephemeral")
    vocab.rate_word(conn, wid, Rating.Good)
    row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    assert row["stability"] is not None
    assert row["difficulty"] is not None
    assert row["state"] in (1, 2, 3)
    assert row["reps"] == 1
    assert row["lapses"] == 0
    assert row["mention_count"] == 1
    assert row["last_used_at"] is not None
    assert row["last_review"] is not None


def test_rate_again_increments_lapses(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "placid")
    wid = _wid(conn, "placid")
    vocab.rate_word(conn, wid, Rating.Good)
    vocab.rate_word(conn, wid, Rating.Again)
    row = conn.execute("SELECT reps, lapses FROM words WHERE id=?", (wid,)).fetchone()
    assert row["reps"] == 2
    assert row["lapses"] == 1


def test_rate_preserves_state_across_calls(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "lucid")
    wid = _wid(conn, "lucid")
    t = datetime.datetime.now(UTC)
    vocab.rate_word(conn, wid, Rating.Good, at=t)
    first = conn.execute(
        "SELECT stability, difficulty FROM words WHERE id=?", (wid,)
    ).fetchone()
    vocab.rate_word(conn, wid, Rating.Good, at=t + datetime.timedelta(days=1))
    second = conn.execute(
        "SELECT stability, difficulty FROM words WHERE id=?", (wid,)
    ).fetchone()
    # Successful review should grow stability.
    assert second["stability"] > first["stability"]


def test_rate_unknown_word_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        vocab.rate_word(conn, 999999, Rating.Good)


# --- compute_weight ----------------------------------------------------------


def test_weight_for_fresh_unrated_word_is_near_eight(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "brandnew")
    row = conn.execute(
        "SELECT * FROM words WHERE text='brandnew'"
    ).fetchone()
    # forget_prob=1, recency≈1, rarity=1 → (1+1)*(1+1)*(1+1) = 8
    w = vocab.compute_weight(row, datetime.datetime.now(UTC))
    assert 7.9 < w <= 8.0


def test_rarity_boost_shrinks_with_mentions(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "seen_often")
    conn.execute(
        "UPDATE words SET mention_count = 9 WHERE text = 'seen_often'"
    )
    row = conn.execute("SELECT * FROM words WHERE text='seen_often'").fetchone()
    # rarity = 1/(1+9) = 0.1 → factor (1+0.1) = 1.1
    # forget_prob=1, recency≈1 → (2)*(2)*(1.1) = 4.4
    w = vocab.compute_weight(row, datetime.datetime.now(UTC))
    assert 4.3 < w < 4.5


def test_recency_boost_decays_with_age(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "aged")
    _set_added(conn, "aged", days_ago=14.0)  # exp(-14/7) = exp(-2) ≈ 0.135
    row = conn.execute("SELECT * FROM words WHERE text='aged'").fetchone()
    # forget_prob=1, recency≈0.135, rarity=1 → 2 * 1.135 * 2 ≈ 4.54
    w = vocab.compute_weight(row, datetime.datetime.now(UTC))
    assert 4.4 < w < 4.7


def test_weight_drops_for_just_learned_word(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "mastered")
    wid = _wid(conn, "mastered")
    vocab.rate_word(conn, wid, Rating.Good)
    row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    # Just rated Good → retrievability ≈ 1, forget_prob ≈ 0 → (1+0)*(…)*(…) << 8
    w = vocab.compute_weight(row, datetime.datetime.now(UTC))
    assert w < 4.5


# --- select_word -------------------------------------------------------------


def test_select_returns_none_when_empty(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    assert vocab.select_word(conn, CHAT) is None


def test_select_returns_only_candidate(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "only_option")
    picked = vocab.select_word(conn, CHAT, rng=random.Random(0))
    assert picked["text"] == "only_option"


def test_select_prefers_new_and_unmentioned_in_aggregate(
    conn: sqlite3.Connection,
) -> None:
    # fresh: just added, zero mentions → weight ≈ 8
    # worn: added 30 days ago, mentioned 9 times → weight ≈ 2 * 1.014 * 1.1 ≈ 2.23
    vocab.add_word(conn, CHAT, "fresh")
    vocab.add_word(conn, CHAT, "worn")
    _set_added(conn, "worn", days_ago=30.0)
    conn.execute("UPDATE words SET mention_count = 9 WHERE text = 'worn'")

    rng = random.Random(42)
    picks = [
        vocab.select_word(conn, CHAT, rng=rng)["text"] for _ in range(400)
    ]
    # With weights ~8 vs ~2.2 the odds are ~3.6:1 in favour of "fresh".
    fresh_count = picks.count("fresh")
    assert fresh_count > picks.count("worn")
    assert fresh_count > 300  # ~78% expected, leaves margin for sampling noise


def test_select_scopes_to_chat(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, 1, "alpha")
    vocab.add_word(conn, 2, "beta")
    picked = vocab.select_word(conn, 1, rng=random.Random(0))
    assert picked["text"] == "alpha"
