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

import csv
import datetime
import html
import io
import logging
import math
import random
import re
import sqlite3
from typing import Callable

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


def add_words_bulk(
    conn: sqlite3.Connection,
    chat_id: int,
    words: list[str],
    translations: list[str | None] | None = None,
) -> dict[str, int]:
    """Bulk-add words. Returns a count breakdown.

    Each item is normalized; entries that normalize to empty count as `invalid`.
    Returns `{"added": N, "skipped": M, "invalid": K}` where `skipped` covers
    both pre-existing rows and within-input duplicates (both manifest as
    `INSERT OR IGNORE` no-ops).

    `translations`, when given, is a parallel list aligned to `words`. Each new
    row gets its translation; for skipped/invalid rows the translation slot is
    discarded. When `translations` is None (default) every new row's
    translation column is NULL — populated lazily by the startup backfill.
    """
    if translations is not None and len(translations) != len(words):
        raise ValueError(
            f"translations length {len(translations)} does not match "
            f"words length {len(words)}"
        )
    ensure_chat(conn, chat_id)
    added = 0
    skipped = 0
    invalid = 0
    now = _now()
    for i, raw in enumerate(words):
        normalized = _normalize(raw)
        if not normalized:
            invalid += 1
            continue
        translation = translations[i] if translations is not None else None
        cur = conn.execute(
            "INSERT OR IGNORE INTO words(chat_id, text, added_at, translation) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, normalized, now, translation),
        )
        if cur.rowcount > 0:
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped, "invalid": invalid}


def set_translation(
    conn: sqlite3.Connection, chat_id: int, text: str, translation: str
) -> None:
    """Update the translation column for `(chat_id, normalized(text))`.

    No-ops when no row matches — callers don't need to know whether the row
    still exists.
    """
    word = _normalize(text)
    if not word:
        return
    conn.execute(
        "UPDATE words SET translation = ? WHERE chat_id = ? AND text = ?",
        (translation, chat_id, word),
    )


def backfill_translations(
    conn: sqlite3.Connection,
    *,
    translate_fn: Callable[[str, str], str],
    log: logging.Logger | None = None,
) -> dict[str, int]:
    """Translate every `words` row whose translation is still NULL.

    Each row is translated using its chat's `translate_target` (joined from
    `chats`). Per-row exceptions are caught, logged via `log.warning(...)` if
    `log` is provided, counted under `failed`, and do not abort the sweep.
    Returns `{"translated": N, "failed": M}`.
    """
    rows = conn.execute(
        "SELECT w.id, w.text, c.translate_target "
        "FROM words w JOIN chats c ON w.chat_id = c.chat_id "
        "WHERE w.translation IS NULL"
    ).fetchall()
    translated = 0
    failed = 0
    for row in rows:
        try:
            result = translate_fn(row["text"], row["translate_target"])
        except Exception as exc:  # noqa: BLE001 — keep the sweep alive
            if log is not None:
                log.warning(
                    "backfill_translations: %r → %r failed: %s",
                    row["text"], row["translate_target"], exc,
                )
            failed += 1
            continue
        conn.execute(
            "UPDATE words SET translation = ? WHERE id = ?",
            (result, row["id"]),
        )
        translated += 1
    return {"translated": translated, "failed": failed}


def parse_csv_words(text: str) -> list[tuple[str, str | None, list[str]]]:
    """Parse a CSV blob into `(text, translation_or_None, labels)` triples.

    Header detection (case-insensitive, whitespace-stripped, only when at
    least one further row follows) selects mode:

    - `text,translation,labels` — three-column mode: each subsequent row's
      second cell is the translation (None when empty/missing) and the third
      cell is a `;`-separated list of label names. Each label fragment is
      stripped and lowercased; empty fragments are dropped; no validation
      happens here (callers validate via `import_rows`).
    - `text,translation` — two-column mode: translation parsed as before;
      labels list is empty for every row.
    - `text` (single cell) — one-column mode.
    - No recognised header — bare list; each row's first cell, translation
      None, labels empty.

    Blank rows and rows whose first cell is empty after stripping are
    skipped. Cells are stripped but NOT lowercased (label fragments are the
    one exception) — `add_words_bulk` does the text normalisation.
    """
    reader = csv.reader(io.StringIO(text))
    rows: list[list[str]] = []
    for row in reader:
        if not row:
            continue
        first = row[0].strip()
        if not first:
            continue
        rows.append([cell.strip() for cell in row])
    if not rows:
        return []
    first_row = rows[0]
    # 3-col header detection is purely structural — header-only short-circuits
    # to [] via the empty rows[1:] loop, so the empty-chat round-trip stays
    # lossless (issue #87).
    is_three_col_header = (
        len(first_row) >= 3
        and first_row[0].lower() == "text"
        and first_row[1].lower() == "translation"
        and first_row[2].lower() == "labels"
    )
    if is_three_col_header:
        out: list[tuple[str, str | None, list[str]]] = []
        for row in rows[1:]:
            text_cell = row[0]
            translation = row[1] if len(row) >= 2 and row[1] else None
            labels_cell = row[2] if len(row) >= 3 else ""
            labels = [
                frag.strip().lower()
                for frag in labels_cell.split(";")
                if frag.strip()
            ]
            out.append((text_cell, translation, labels))
        return out
    is_two_col_header = (
        len(rows) >= 2
        and len(first_row) >= 2
        and first_row[0].lower() == "text"
        and first_row[1].lower() == "translation"
    )
    if is_two_col_header:
        two: list[tuple[str, str | None, list[str]]] = []
        for row in rows[1:]:
            text_cell = row[0]
            translation = row[1] if len(row) >= 2 and row[1] else None
            two.append((text_cell, translation, []))
        return two
    if len(rows) >= 2 and first_row[0].lower() == "text":
        rows = rows[1:]
    return [(row[0], None, []) for row in rows]


def format_csv(rows: list[tuple[str, str | None, list[str]]]) -> str:
    """Serialize `(text, translation, labels)` triples as a CSV string.

    Header `text,translation,labels` is always emitted. Rows are sorted
    case-insensitively by text for stable diffs. A None translation writes
    an empty second cell — never the literal string `"None"`. The labels
    list is joined with `;` in the order given (callers are expected to
    sort if they want stable ordering); an empty list writes an empty third
    cell. Uses `\\n` line terminators for predictable cross-platform
    output.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["text", "translation", "labels"])
    for text, translation, labels in sorted(rows, key=lambda r: r[0].lower()):
        writer.writerow([
            text,
            translation if translation is not None else "",
            ";".join(labels),
        ])
    return buf.getvalue()


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
    names: list[str] | None = None,
    rng: random.Random | None = None,
    now: datetime.datetime | None = None,
) -> sqlite3.Row | None:
    """Sample one word for this chat using the weighted formula. None if empty.

    When `names` is non-empty, the candidate pool is restricted to words
    tagged with **all** of those label names (AND semantics, via
    `words_matching_labels`). An empty list or None preserves the unfiltered
    pool.
    """
    now = now or _now_dt()
    if names:
        rows = words_matching_labels(conn, chat_id, names)
    else:
        rows = conn.execute(
            "SELECT * FROM words WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    if not rows:
        return None
    weights = [compute_weight(r, now) for r in rows]
    pick = (rng or random).choices(rows, weights=weights, k=1)
    return pick[0]


# --- labels (per-chat many-to-many tags on words) ---------------------------

POS_PREFIX = "pos:"


def parse_label_spec(args: list[str]) -> list[str]:
    """Tokenise a Telegram-args list into normalised label names.

    Each token is stripped and lowercased. A token is valid if it is either a
    bare string (`medicine`) or `key:value` (`pos:noun`). Order is preserved
    and duplicates are dropped (first occurrence wins). Raises `ValueError`
    listing every offending raw token if any token is empty, has internal
    whitespace, or has an empty key/value or more than one colon.

    Shared by `/label`, `/unlabel`, and the upcoming `/list`, `/games`,
    `/focus` filter wirings — single source of truth for the spec format.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    bad: list[str] = []
    for raw in args:
        token = raw.strip().lower()
        if not token or any(c.isspace() for c in token):
            bad.append(raw)
            continue
        parts = token.split(":")
        if len(parts) > 2 or any(p == "" for p in parts):
            bad.append(raw)
            continue
        if token in seen_set:
            continue
        seen.append(token)
        seen_set.add(token)
    if bad:
        raise ValueError("malformed label spec: " + " ".join(bad))
    return seen


def find_word_id(
    conn: sqlite3.Connection, chat_id: int, text: str
) -> int | None:
    """Return `words.id` for `(chat_id, _normalize(text))`, or None.

    Mirrors `/remove`'s case-insensitive lookup so `/label`/`/unlabel` accept
    the same forms users already type.
    """
    word = _normalize(text)
    if not word:
        return None
    row = conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?",
        (chat_id, word),
    ).fetchone()
    return row["id"] if row else None


def get_or_create_label(conn: sqlite3.Connection, chat_id: int, name: str) -> int:
    """Return the id of `(chat_id, name)`, inserting the row if needed.

    Idempotent: calling twice with the same `(chat_id, name)` returns the same
    id and leaves a single row in `labels`.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO labels(chat_id, name) VALUES (?, ?)",
        (chat_id, name),
    )
    if cur.rowcount > 0:
        return cur.lastrowid
    row = conn.execute(
        "SELECT id FROM labels WHERE chat_id = ? AND name = ?",
        (chat_id, name),
    ).fetchone()
    return row["id"]


def attach_label(conn: sqlite3.Connection, word_id: int, label_id: int) -> bool:
    """Attach `label_id` to `word_id`. Returns True iff a new row was inserted.

    Raises `KeyError(label_id)` when the label does not exist.

    Enforces the "one POS per word" rule: when the label's name starts with
    `pos:`, any other `pos:*` row on this word is detached first. The
    detach + insert run inside an explicit transaction so observers never see
    a state where both old and new `pos:*` are simultaneously attached.
    """
    label = conn.execute(
        "SELECT name FROM labels WHERE id = ?", (label_id,)
    ).fetchone()
    if label is None:
        raise KeyError(label_id)
    is_pos = label["name"].startswith(POS_PREFIX)
    conn.execute("BEGIN")
    try:
        if is_pos:
            conn.execute(
                "DELETE FROM word_labels "
                "WHERE word_id = ? "
                "  AND label_id != ? "
                "  AND label_id IN (SELECT id FROM labels WHERE name LIKE ?)",
                (word_id, label_id, f"{POS_PREFIX}%"),
            )
        cur = conn.execute(
            "INSERT OR IGNORE INTO word_labels(word_id, label_id) VALUES (?, ?)",
            (word_id, label_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount > 0


def detach_label(conn: sqlite3.Connection, word_id: int, label_id: int) -> bool:
    """Detach `label_id` from `word_id`. Returns True iff a row was deleted."""
    cur = conn.execute(
        "DELETE FROM word_labels WHERE word_id = ? AND label_id = ?",
        (word_id, label_id),
    )
    return cur.rowcount > 0


def labels_for_word(conn: sqlite3.Connection, word_id: int) -> list[str]:
    """Names of every label attached to `word_id`, sorted ascending."""
    rows = conn.execute(
        "SELECT l.name FROM labels l "
        "JOIN word_labels wl ON wl.label_id = l.id "
        "WHERE wl.word_id = ? "
        "ORDER BY l.name ASC",
        (word_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def labels_for_words_in_chat(
    conn: sqlite3.Connection, chat_id: int
) -> dict[int, list[str]]:
    """Map every word in `chat_id` that has ≥1 label to its sorted label names.

    One query — used by `/export` to avoid an N+1 over `labels_for_word`.
    Words with no labels are absent from the result; callers default to `[]`.
    """
    rows = conn.execute(
        "SELECT wl.word_id AS word_id, l.name AS name "
        "FROM word_labels wl "
        "JOIN labels l ON l.id = wl.label_id "
        "JOIN words w ON w.id = wl.word_id "
        "WHERE w.chat_id = ? "
        "ORDER BY wl.word_id, l.name ASC",
        (chat_id,),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["word_id"], []).append(r["name"])
    return out


def import_rows(
    conn: sqlite3.Connection,
    chat_id: int,
    rows: list[tuple[str, str | None, list[str]]],
) -> dict[str, object]:
    """Import `(text, translation, labels)` triples with per-row label handling.

    For each row, in order:

    - normalise the text; an empty result counts toward `invalid` and the
      row is skipped.
    - validate the row's label list via `parse_label_spec` (catches malformed
      tokens like `:noun`, `pos:`, `pos:a:b`); if validation fails, the row
      counts toward `rejected` with a `(row_idx, message)` entry in
      `label_errors` and neither the word nor any labels are inserted.
    - if the validated label list contains more than one distinct `pos:*`
      token, the row is `rejected` with a multi-POS message; word is not
      inserted.
    - otherwise, insert the word (`INSERT OR IGNORE` — duplicates count
      toward `skipped` and keep their existing translation) and attach each
      label additively via `get_or_create_label` + `attach_label`. Labels
      are merged into existing words; existing labels are preserved.

    Returns `{"added": int, "skipped": int, "invalid": int, "rejected": int,
    "label_errors": list[(int, str)]}`. `row_idx` is 1-based and counts
    every input row, including ones rejected for any reason.
    """
    ensure_chat(conn, chat_id)
    added = 0
    skipped = 0
    invalid = 0
    rejected = 0
    label_errors: list[tuple[int, str]] = []
    now = _now()
    for i, (raw_text, translation, raw_labels) in enumerate(rows, start=1):
        normalized = _normalize(raw_text)
        if not normalized:
            invalid += 1
            continue
        try:
            labels = parse_label_spec(list(raw_labels))
        except ValueError as exc:
            rejected += 1
            label_errors.append((i, str(exc)))
            continue
        pos_labels = [n for n in labels if n.startswith(POS_PREFIX)]
        if len(pos_labels) > 1:
            rejected += 1
            label_errors.append(
                (i, "multiple POS labels: " + ", ".join(pos_labels))
            )
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO words(chat_id, text, added_at, translation) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, normalized, now, translation),
        )
        if cur.rowcount > 0:
            added += 1
            word_id = cur.lastrowid
        else:
            skipped += 1
            row = conn.execute(
                "SELECT id FROM words WHERE chat_id = ? AND text = ?",
                (chat_id, normalized),
            ).fetchone()
            word_id = row["id"]
        for name in labels:
            label_id = get_or_create_label(conn, chat_id, name)
            attach_label(conn, word_id, label_id)
    return {
        "added": added,
        "skipped": skipped,
        "invalid": invalid,
        "rejected": rejected,
        "label_errors": label_errors,
    }


def labels_with_counts(
    conn: sqlite3.Connection, chat_id: int
) -> list[tuple[str, int]]:
    """All labels for `chat_id` paired with their attached-word count.

    Sorted alphabetically by name; labels with no attached words still appear
    with count 0 (LEFT JOIN).
    """
    rows = conn.execute(
        "SELECT l.name AS name, COUNT(wl.word_id) AS n "
        "FROM labels l "
        "LEFT JOIN word_labels wl ON wl.label_id = l.id "
        "WHERE l.chat_id = ? "
        "GROUP BY l.id "
        "ORDER BY l.name ASC",
        (chat_id,),
    ).fetchall()
    return [(r["name"], r["n"]) for r in rows]


def find_label_id(
    conn: sqlite3.Connection, chat_id: int, name: str
) -> int | None:
    """Return `labels.id` for `(chat_id, name)` or None — never inserts."""
    row = conn.execute(
        "SELECT id FROM labels WHERE chat_id = ? AND name = ?",
        (chat_id, name),
    ).fetchone()
    return row["id"] if row else None


def words_matching_labels(
    conn: sqlite3.Connection,
    chat_id: int,
    names: list[str],
) -> list[sqlite3.Row]:
    """Words for `chat_id` tagged with **all** of `names` (AND semantics).

    Duplicate entries in `names` are deduped — `["pos:noun", "pos:noun"]`
    matches the same words as `["pos:noun"]`. An empty `names` returns every
    word for the chat (no filter). Result ordering matches `list_words`:
    mention_count ASC, added_at DESC.
    """
    if not names:
        return conn.execute(
            "SELECT * FROM words WHERE chat_id = ? "
            "ORDER BY mention_count ASC, added_at DESC",
            (chat_id,),
        ).fetchall()
    unique_names = list(dict.fromkeys(names))
    placeholders = ",".join("?" * len(unique_names))
    return conn.execute(
        f"SELECT w.* FROM words w "
        f"JOIN word_labels wl ON wl.word_id = w.id "
        f"JOIN labels l ON l.id = wl.label_id "
        f"WHERE w.chat_id = ? AND l.chat_id = ? AND l.name IN ({placeholders}) "
        f"GROUP BY w.id "
        f"HAVING COUNT(DISTINCT l.name) = ? "
        f"ORDER BY w.mention_count ASC, w.added_at DESC",
        (chat_id, chat_id, *unique_names, len(unique_names)),
    ).fetchall()


# --- focus (sticky per-chat label spec) -------------------------------------


def get_focus_spec(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """Return the chat's stored focus spec or None when unset / chat missing.

    The stored value is the normalised space-joined output of
    `parse_label_spec` — a `.split()` recovers the list of names verbatim.
    """
    row = conn.execute(
        "SELECT focus_spec FROM chats WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if row is None:
        return None
    return row["focus_spec"]


def set_focus_spec(
    conn: sqlite3.Connection, chat_id: int, spec_text: str | None
) -> None:
    """Upsert the chat's focus spec; passing None clears it.

    Ensures a `chats` row exists so `/focus` can be set before `/start`.
    """
    ensure_chat(conn, chat_id)
    conn.execute(
        "UPDATE chats SET focus_spec = ? WHERE chat_id = ?",
        (spec_text, chat_id),
    )
