"""Tests for vocab.py — CRUD + mention scanning."""

from __future__ import annotations

import sqlite3

import pytest

import vocab


CHAT = 100


def _all_words(conn: sqlite3.Connection) -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM words").fetchall()]


def test_count_words_is_per_chat(conn: sqlite3.Connection) -> None:
    assert vocab.count_words(conn, CHAT) == 0
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    vocab.add_word(conn, CHAT + 1, "gamma")  # different chat
    assert vocab.count_words(conn, CHAT) == 2
    assert vocab.count_words(conn, CHAT + 1) == 1
    assert vocab.count_words(conn, 999) == 0


def test_add_word_returns_true_for_new(conn: sqlite3.Connection) -> None:
    assert vocab.add_word(conn, CHAT, "ephemeral") is True
    assert _all_words(conn) == ["ephemeral"]


def test_add_word_returns_false_for_duplicate(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "ephemeral")
    assert vocab.add_word(conn, CHAT, "ephemeral") is False
    assert len(_all_words(conn)) == 1


def test_add_word_normalizes_case_and_whitespace(conn: sqlite3.Connection) -> None:
    assert vocab.add_word(conn, CHAT, "  Serendipity  ") is True
    assert vocab.add_word(conn, CHAT, "SERENDIPITY") is False
    assert _all_words(conn) == ["serendipity"]


def test_add_empty_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        vocab.add_word(conn, CHAT, "   ")


def test_add_word_auto_creates_chat(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "placid")
    row = conn.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (CHAT,)).fetchone()
    assert row is not None


def test_remove_word_returns_true_when_present(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "placid")
    assert vocab.remove_word(conn, CHAT, "placid") is True
    assert _all_words(conn) == []


def test_remove_word_returns_false_when_absent(conn: sqlite3.Connection) -> None:
    assert vocab.remove_word(conn, CHAT, "nope") is False


def test_remove_word_is_case_insensitive(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "Placid")
    assert vocab.remove_word(conn, CHAT, "PLACID") is True


def test_list_all_orders_by_mention_count_then_recent(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "old_high")
    vocab.add_word(conn, CHAT, "old_low")
    vocab.add_word(conn, CHAT, "new_low")

    conn.execute("UPDATE words SET mention_count = 5 WHERE text = 'old_high'")
    # Push old_low's added_at earlier so new_low sorts above it for same count.
    conn.execute("UPDATE words SET added_at = '2020-01-01' WHERE text = 'old_low'")

    rows = vocab.list_words(conn, CHAT)
    # Expected: both count=0 words first (newest first), then count=5.
    assert [r["text"] for r in rows] == ["new_low", "old_low", "old_high"]


def test_list_filters_by_substring(conn: sqlite3.Connection) -> None:
    for w in ("cat", "concatenate", "dog"):
        vocab.add_word(conn, CHAT, w)
    rows = vocab.list_words(conn, CHAT, contains="cat")
    assert sorted(r["text"] for r in rows) == ["cat", "concatenate"]


def test_list_scopes_to_chat(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, 1, "alpha")
    vocab.add_word(conn, 2, "beta")
    rows = vocab.list_words(conn, 1)
    assert [r["text"] for r in rows] == ["alpha"]


def test_scan_mentions_matches_case_insensitive_substrings() -> None:
    words = [(1, "ephemeral"), (2, "serendipity"), (3, "placid")]
    text = "It was a Serendipity moment, brief and EPHEMERAL."
    assert sorted(vocab.scan_mentions(text, words)) == [1, 2]


def test_scan_mentions_matches_phrases() -> None:
    words = [(1, "on the fly"), (2, "by the book")]
    text = "He figured it out on the fly, no rehearsal."
    assert vocab.scan_mentions(text, words) == [1]


def test_scan_mentions_counts_each_word_once() -> None:
    words = [(1, "cat")]
    text = "cat cat cat cat"
    assert vocab.scan_mentions(text, words) == [1]


def test_scan_mentions_empty_inputs() -> None:
    assert vocab.scan_mentions("", []) == []
    assert vocab.scan_mentions("hello world", []) == []
    assert vocab.scan_mentions("", [(1, "hello")]) == []


def test_highlight_matches_empty_text() -> None:
    assert vocab.highlight_matches("", ["foo"]) == ""


def test_highlight_matches_no_words_just_escapes() -> None:
    assert vocab.highlight_matches("a < b & c", []) == "a &lt; b &amp; c"


def test_highlight_matches_wraps_substring_case_insensitive() -> None:
    out = vocab.highlight_matches("It was Serendipity, brief.", ["serendipity"])
    assert out == "It was <code>Serendipity</code>, brief."


def test_highlight_matches_escapes_html_chars_outside_match() -> None:
    out = vocab.highlight_matches("<cat> & dog", ["cat"])
    assert out == "&lt;<code>cat</code>&gt; &amp; dog"


def test_highlight_matches_longer_word_wins_on_overlap() -> None:
    out = vocab.highlight_matches("concatenate things", ["cat", "concatenate"])
    assert out == "<code>concatenate</code> things"


def test_highlight_matches_multiple_non_overlapping() -> None:
    out = vocab.highlight_matches("cat dog cat", ["cat", "dog"])
    assert out == "<code>cat</code> <code>dog</code> <code>cat</code>"


def test_highlight_matches_phrase() -> None:
    out = vocab.highlight_matches("She did it on the fly today.", ["on the fly"])
    assert out == "She did it <code>on the fly</code> today."


def test_highlight_matches_ignores_empty_word_entries() -> None:
    assert (
        vocab.highlight_matches("hello world", ["", "world"])
        == "hello <code>world</code>"
    )


def test_highlight_matches_strips_markdown_bold_around_vocab_word() -> None:
    out = vocab.highlight_matches(
        "It's a testament to **strength** today.", ["strength"]
    )
    assert out == "It&#x27;s a testament to <code>strength</code> today."


def test_highlight_matches_strips_markdown_bold_when_word_not_in_vocab() -> None:
    out = vocab.highlight_matches("truly **remarkable** indeed", [])
    assert out == "truly remarkable indeed"


def test_highlight_matches_strips_underscore_markdown_bold() -> None:
    out = vocab.highlight_matches("__placid__ waters", ["placid"])
    assert out == "<code>placid</code> waters"


def test_highlight_matches_handles_multiple_markdown_chunks() -> None:
    out = vocab.highlight_matches("**alpha** then **beta** end", ["beta"])
    assert out == "alpha then <code>beta</code> end"


def test_bump_mentions_increments_and_timestamps(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "placid")
    wid = conn.execute("SELECT id FROM words").fetchone()["id"]

    vocab.bump_mentions(conn, [wid])
    vocab.bump_mentions(conn, [wid])

    row = conn.execute(
        "SELECT mention_count, last_used_at FROM words WHERE id = ?", (wid,)
    ).fetchone()
    assert row["mention_count"] == 2
    assert row["last_used_at"] is not None


def test_bump_mentions_noop_on_empty_list(conn: sqlite3.Connection) -> None:
    vocab.bump_mentions(conn, [])  # must not raise


def test_ensure_chat_is_idempotent(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, 42, tz="Europe/Warsaw")
    vocab.ensure_chat(conn, 42, tz="UTC")  # second call must not overwrite
    row = conn.execute("SELECT tz FROM chats WHERE chat_id = 42").fetchone()
    assert row["tz"] == "Europe/Warsaw"
