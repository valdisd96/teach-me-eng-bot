"""Vocabulary CRUD, literal mention scanning, FSRS rating, and word selection.

Words are normalized to lowercased, stripped form on write and scanned
case-insensitively. Substring (literal) matching is intentional for this stage
— no lemmatization, no stemming.

FSRS drives per-word memory state (stability/difficulty/due via py-fsrs with
desired_retention=0.95 and maximum_interval=7d so reviews stay tight). Push
selection samples a word using a multiplicative weight:

    weight = (1 + forget_prob) * (1 + recency_boost) * (1 + rarity_boost)

so no single signal dominates and randomness is baked into `random.choices`.
"""

from __future__ import annotations

import datetime
import html
import math
import random
import re
import sqlite3

from fsrs import Card, Rating, Scheduler, State


FSRS_RETENTION = 0.95
FSRS_MAX_DAYS = 7
RECENCY_TAU_DAYS = 7.0

# enable_fuzzing=False → deterministic intervals, easier to test and reason about.
_SCHEDULER = Scheduler(
    desired_retention=FSRS_RETENTION,
    maximum_interval=FSRS_MAX_DAYS,
    enable_fuzzing=False,
)


_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _now_dt() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now() -> str:
    return _now_dt().strftime(_TS_FMT)


def _parse_ts(s: str) -> datetime.datetime:
    """Parse both our short UTC format and py-fsrs's ISO-8601 strings."""
    try:
        return datetime.datetime.strptime(s, _TS_FMT).replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return datetime.datetime.fromisoformat(s)


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


def count_words(conn: sqlite3.Connection, chat_id: int) -> int:
    """Return how many vocab rows exist for `chat_id`."""
    row = conn.execute(
        "SELECT COUNT(*) FROM words WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return row[0] if row else 0


def scan_mentions(text: str, words: list[tuple[int, str]]) -> list[int]:
    """Return ids of vocab words that appear as literal substrings in `text`.

    Case-insensitive. Stored words are assumed already-normalized. Each word
    contributes to the result at most once regardless of occurrence count.
    """
    haystack = text.lower()
    return [wid for wid, w in words if w and w in haystack]


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)


def highlight_matches(text: str, words: list[str]) -> str:
    """HTML-escape `text` and wrap any vocab-word substrings in <code>…</code>.

    Mirrors `scan_mentions`'s case-insensitive substring match. When two
    candidate words would overlap (e.g. "cat" inside "concatenate") the
    longer one wins, so we never highlight a fragment of a longer match.
    Markdown bold markers (`**x**` / `__x__`) the model sometimes emits are
    stripped first — Telegram's HTML mode would otherwise render them
    literally. Returns a string safe to send with Telegram's HTML parse mode.
    """
    if not text:
        return ""
    text = _MD_BOLD_RE.sub(lambda m: m.group(1) or m.group(2), text)
    haystack = text.lower()
    spans: list[tuple[int, int]] = []
    for w in sorted({w for w in words if w}, key=len, reverse=True):
        i = 0
        while True:
            idx = haystack.find(w, i)
            if idx == -1:
                break
            end = idx + len(w)
            i = end
            if any(s < end and idx < e for s, e in spans):
                continue
            spans.append((idx, end))
    spans.sort()
    out: list[str] = []
    cursor = 0
    for s, e in spans:
        out.append(html.escape(text[cursor:s]))
        out.append("<code>")
        out.append(html.escape(text[s:e]))
        out.append("</code>")
        cursor = e
    out.append(html.escape(text[cursor:]))
    return "".join(out)


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


def _card_from_row(row: sqlite3.Row) -> Card:
    """Reconstruct a py-fsrs Card from a word row, or a fresh one if unrated."""
    if row["stability"] is None:
        return Card()
    return Card(
        state=State(row["state"]) if row["state"] else State.Learning,
        step=row["step"],
        stability=row["stability"],
        difficulty=row["difficulty"],
        due=_parse_ts(row["due"]) if row["due"] else None,
        last_review=_parse_ts(row["last_review"]) if row["last_review"] else None,
    )


def rate_word(
    conn: sqlite3.Connection,
    word_id: int,
    rating: Rating,
    *,
    at: datetime.datetime | None = None,
) -> None:
    """Apply an FSRS rating, bump mention_count, and stamp last_used_at.

    `at` pins the review time (useful for tests / backfilled pushes); defaults
    to now.
    """
    at = at or _now_dt()
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        raise KeyError(word_id)
    card = _card_from_row(row)
    new_card, _log = _SCHEDULER.review_card(card, rating, review_datetime=at)
    lapses = row["lapses"] + (1 if rating == Rating.Again else 0)
    conn.execute(
        "UPDATE words SET "
        "stability = ?, difficulty = ?, state = ?, step = ?, due = ?, "
        "reps = reps + 1, lapses = ?, last_review = ?, "
        "mention_count = mention_count + 1, last_used_at = ? "
        "WHERE id = ?",
        (
            new_card.stability,
            new_card.difficulty,
            new_card.state.value,
            new_card.step,
            new_card.due.isoformat() if new_card.due else None,
            lapses,
            new_card.last_review.isoformat() if new_card.last_review else None,
            at.strftime(_TS_FMT),
            word_id,
        ),
    )


def _forget_prob(row: sqlite3.Row, now: datetime.datetime) -> float:
    """1 - FSRS retrievability. Unrated words → 1.0 (maximal urgency)."""
    if row["stability"] is None:
        return 1.0
    return 1.0 - _SCHEDULER.get_card_retrievability(
        _card_from_row(row), current_datetime=now
    )


def compute_weight(row: sqlite3.Row, now: datetime.datetime) -> float:
    """Selection weight for a word. Each factor lives in [0, 1] and is lifted
    to [1, 2] so no single signal can dominate the product."""
    forget_prob = _forget_prob(row, now)
    age_days = (now - _parse_ts(row["added_at"])).total_seconds() / 86400.0
    recency_boost = math.exp(-max(age_days, 0.0) / RECENCY_TAU_DAYS)
    rarity_boost = 1.0 / (1.0 + row["mention_count"])
    return (1 + forget_prob) * (1 + recency_boost) * (1 + rarity_boost)


def compute_scores(
    rows: list[sqlite3.Row],
    now: datetime.datetime | None = None,
) -> list[int]:
    """Normalize per-row weights to 0..100 integers against the list's max.

    Empty input returns []. If all weights tie, every row scores 100.
    """
    if not rows:
        return []
    now = now or _now_dt()
    weights = [compute_weight(r, now) for r in rows]
    top = max(weights)
    if top <= 0:
        return [0] * len(rows)
    return [round(w / top * 100) for w in weights]


def select_word(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    rng: random.Random | None = None,
    now: datetime.datetime | None = None,
) -> sqlite3.Row | None:
    """Sample one word for this chat using the weighted formula. None if empty."""
    now = now or _now_dt()
    rows = conn.execute(
        "SELECT * FROM words WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    if not rows:
        return None
    weights = [compute_weight(r, now) for r in rows]
    pick = (rng or random).choices(rows, weights=weights, k=1)
    return pick[0]
