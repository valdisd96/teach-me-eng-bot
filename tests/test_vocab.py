"""Tests for vocab.py — CRUD + mention scanning."""

from __future__ import annotations

import logging
import sqlite3

import pytest

import vocab


CHAT = 100


def _all_words(conn: sqlite3.Connection) -> list[str]:
    return [r["text"] for r in conn.execute("SELECT text FROM words").fetchall()]


def _set_chat_target(conn: sqlite3.Connection, chat_id: int, target: str) -> None:
    vocab.ensure_chat(conn, chat_id)
    conn.execute(
        "UPDATE chats SET translate_target = ? WHERE chat_id = ?", (target, chat_id)
    )


def _translation_for(
    conn: sqlite3.Connection, chat_id: int, text: str
) -> str | None:
    row = conn.execute(
        "SELECT translation FROM words WHERE chat_id = ? AND text = ?",
        (chat_id, text),
    ).fetchone()
    return None if row is None else row["translation"]


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


# --- bulk add / CSV import-export ------------------------------------------


def test_add_words_bulk_inserts_new_and_normalizes(conn: sqlite3.Connection) -> None:
    counts = vocab.add_words_bulk(conn, CHAT, ["Apple", " banana ", "CHERRY"])
    assert counts == {"added": 3, "skipped": 0, "invalid": 0}
    assert sorted(_all_words(conn)) == ["apple", "banana", "cherry"]


def test_add_words_bulk_counts_duplicates_against_existing(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "apple")
    counts = vocab.add_words_bulk(conn, CHAT, ["apple", "banana"])
    assert counts == {"added": 1, "skipped": 1, "invalid": 0}


def test_add_words_bulk_dedupes_within_input(conn: sqlite3.Connection) -> None:
    counts = vocab.add_words_bulk(conn, CHAT, ["apple", "Apple", "  apple  "])
    # First is added; the rest collide with the row inserted in the same call.
    assert counts == {"added": 1, "skipped": 2, "invalid": 0}
    assert _all_words(conn) == ["apple"]


def test_add_words_bulk_counts_blank_entries_as_invalid(conn: sqlite3.Connection) -> None:
    counts = vocab.add_words_bulk(conn, CHAT, ["", "   ", "apple"])
    assert counts == {"added": 1, "skipped": 0, "invalid": 2}
    assert _all_words(conn) == ["apple"]


def test_add_words_bulk_empty_input(conn: sqlite3.Connection) -> None:
    counts = vocab.add_words_bulk(conn, CHAT, [])
    assert counts == {"added": 0, "skipped": 0, "invalid": 0}


def test_add_words_bulk_auto_creates_chat(conn: sqlite3.Connection) -> None:
    vocab.add_words_bulk(conn, 7777, ["alpha"])
    row = conn.execute("SELECT chat_id FROM chats WHERE chat_id = 7777").fetchone()
    assert row is not None


def test_add_words_bulk_scopes_to_chat(conn: sqlite3.Connection) -> None:
    vocab.add_word(conn, CHAT, "apple")
    counts = vocab.add_words_bulk(conn, CHAT + 1, ["apple"])
    # Same text in a different chat is a fresh row, not a duplicate.
    assert counts == {"added": 1, "skipped": 0, "invalid": 0}


def test_parse_csv_words_drops_text_header() -> None:  # AC2-legacy-header (issue #64)
    assert vocab.parse_csv_words("text\napple\nbanana\n") == [
        ("apple", None),
        ("banana", None),
    ]


def test_parse_csv_words_header_detection_is_case_insensitive() -> None:
    assert vocab.parse_csv_words("TEXT\napple\n") == [("apple", None)]


def test_parse_csv_words_keeps_header_when_only_row() -> None:
    # A bare list of one line where that line is "text" must not be eaten as a header.
    assert vocab.parse_csv_words("text\n") == [("text", None)]


def test_parse_csv_words_accepts_bare_list_without_header() -> None:  # AC2-legacy-bare (issue #64)
    assert vocab.parse_csv_words("apple\nbanana\ncherry\n") == [
        ("apple", None),
        ("banana", None),
        ("cherry", None),
    ]


def test_parse_csv_words_skips_blank_rows_and_blank_first_cells() -> None:
    text = "apple\n\n , extra\nbanana\n"
    # row 2 is fully empty; row 3's first cell is blank (after strip).
    assert vocab.parse_csv_words(text) == [("apple", None), ("banana", None)]


def test_parse_csv_words_returns_first_column_only() -> None:
    text = "text,note\napple,fruit\nbanana,yellow\n"
    # `note` is not `translation`, so legacy mode wins and the second column
    # is dropped. Translations come back as None.
    assert vocab.parse_csv_words(text) == [("apple", None), ("banana", None)]


def test_parse_csv_words_strips_whitespace_but_preserves_case() -> None:
    # Casing is preserved here; lowercasing happens in add_words_bulk via _normalize.
    assert vocab.parse_csv_words("  Apple  \n  BANANA\n") == [
        ("Apple", None),
        ("BANANA", None),
    ]


def test_parse_csv_words_empty_input() -> None:
    assert vocab.parse_csv_words("") == []


def test_format_csv_emits_header_and_sorts_case_insensitively() -> None:
    out = vocab.format_csv([("banana", None), ("Apple", None), ("cherry", None)])
    assert out == "text,translation\nApple,\nbanana,\ncherry,\n"


def test_format_csv_empty_list_still_writes_header() -> None:
    assert vocab.format_csv([]) == "text,translation\n"


def test_format_csv_uses_lf_line_terminator() -> None:
    out = vocab.format_csv([("apple", None)])
    assert "\r" not in out
    assert out.endswith("\n")


def test_format_csv_quotes_cells_with_csv_special_characters() -> None:
    # csv module must escape commas/quotes so the output round-trips cleanly.
    out = vocab.format_csv([("needs, comma", None), ('has "quote"', None)])
    assert vocab.parse_csv_words(out) == [
        ('has "quote"', None),
        ("needs, comma", None),
    ]


def test_csv_round_trip_preserves_words() -> None:
    rows = [("apple", None), ("banana split", None), ("cherry", None)]
    serialized = vocab.format_csv(rows)
    assert sorted(vocab.parse_csv_words(serialized)) == sorted(rows)


# --- set_translation (issue #63) -------------------------------------------


def test_set_translation_writes_for_existing_row(conn: sqlite3.Connection) -> None:  # AC3 — persist for matching row
    vocab.add_word(conn, CHAT, "apple")
    vocab.set_translation(conn, CHAT, "apple", "яблоко")
    assert _translation_for(conn, CHAT, "apple") == "яблоко"


def test_set_translation_normalizes_text_for_lookup(conn: sqlite3.Connection) -> None:  # AC3 — _normalize gates the WHERE
    vocab.add_word(conn, CHAT, "apple")
    # Pre-norm whitespace + casing must still target the same row.
    vocab.set_translation(conn, CHAT, "  APPLE  ", "яблоко")
    assert _translation_for(conn, CHAT, "apple") == "яблоко"


def test_set_translation_noop_when_row_missing(conn: sqlite3.Connection) -> None:  # AC3 — never raises for missing row
    # No add_word call: there is no matching row in this chat.
    vocab.ensure_chat(conn, CHAT)
    vocab.set_translation(conn, CHAT, "ghost", "призрак")  # must not raise
    row = conn.execute(
        "SELECT 1 FROM words WHERE chat_id = ? AND text = 'ghost'", (CHAT,)
    ).fetchone()
    assert row is None, "set_translation must not insert"


def test_set_translation_does_not_disturb_other_chats(conn: sqlite3.Connection) -> None:  # AC3 — chat_id scopes the UPDATE
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT + 1, "apple")
    vocab.set_translation(conn, CHAT, "apple", "яблоко")
    assert _translation_for(conn, CHAT, "apple") == "яблоко"
    assert _translation_for(conn, CHAT + 1, "apple") is None, (
        "translation must scope to chat_id"
    )


# --- backfill_translations (issue #63) -------------------------------------


def test_backfill_translates_only_null_rows(conn: sqlite3.Connection) -> None:  # AC2 — NULL rows get translated
    _set_chat_target(conn, CHAT, "ru")
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "banana")

    counts = vocab.backfill_translations(
        conn, translate_fn=lambda text, target: f"{text}->{target}"
    )

    assert counts == {"translated": 2, "failed": 0}
    assert _translation_for(conn, CHAT, "apple") == "apple->ru"
    assert _translation_for(conn, CHAT, "banana") == "banana->ru"


def test_backfill_leaves_non_null_rows_untouched(conn: sqlite3.Connection) -> None:  # AC2 — pre-translated rows are skipped
    _set_chat_target(conn, CHAT, "ru")
    vocab.add_word(conn, CHAT, "apple")
    vocab.set_translation(conn, CHAT, "apple", "preset")
    vocab.add_word(conn, CHAT, "banana")  # NULL

    seen: list[tuple[str, str]] = []

    def fake_translate(text: str, target: str) -> str:
        seen.append((text, target))
        return f"{text}!"

    counts = vocab.backfill_translations(conn, translate_fn=fake_translate)

    assert counts == {"translated": 1, "failed": 0}, f"got {counts}"
    assert seen == [("banana", "ru")], (
        f"translate_fn must only be called for NULL rows; got {seen}"
    )
    assert _translation_for(conn, CHAT, "apple") == "preset"
    assert _translation_for(conn, CHAT, "banana") == "banana!"


def test_backfill_uses_per_chat_translate_target(conn: sqlite3.Connection) -> None:  # AC2 — edge: multiple chats different targets
    _set_chat_target(conn, CHAT, "ru")
    _set_chat_target(conn, CHAT + 1, "es")
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT + 1, "apple")

    counts = vocab.backfill_translations(
        conn, translate_fn=lambda text, target: f"{text}-{target}"
    )

    assert counts == {"translated": 2, "failed": 0}
    assert _translation_for(conn, CHAT, "apple") == "apple-ru"
    assert _translation_for(conn, CHAT + 1, "apple") == "apple-es"


def test_backfill_swallows_per_row_exception(conn: sqlite3.Connection) -> None:  # AC2 — per-row failure leaves row NULL, sweep continues
    _set_chat_target(conn, CHAT, "ru")
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "banana")
    vocab.add_word(conn, CHAT, "cherry")

    def flaky(text: str, target: str) -> str:
        if text == "banana":
            raise RuntimeError("boom")
        return f"{text}-{target}"

    counts = vocab.backfill_translations(conn, translate_fn=flaky)

    assert counts == {"translated": 2, "failed": 1}, f"got {counts}"
    assert _translation_for(conn, CHAT, "apple") == "apple-ru"
    assert _translation_for(conn, CHAT, "banana") is None, (
        "failed row must remain NULL"
    )
    assert _translation_for(conn, CHAT, "cherry") == "cherry-ru"


def test_backfill_logs_warning_when_logger_provided(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:  # AC2 — error path: log.warning emitted for failures
    _set_chat_target(conn, CHAT, "ru")
    vocab.add_word(conn, CHAT, "apple")

    log = logging.getLogger("test_backfill_warns")

    def boom(text: str, target: str) -> str:
        raise RuntimeError("nope")

    with caplog.at_level(logging.WARNING, logger=log.name):
        counts = vocab.backfill_translations(conn, translate_fn=boom, log=log)

    assert counts == {"translated": 0, "failed": 1}
    assert any(
        rec.levelno == logging.WARNING for rec in caplog.records
    ), f"expected a WARNING record, got {[r.levelname for r in caplog.records]}"


def test_backfill_no_logger_swallows_exception_silently(
    conn: sqlite3.Connection,
) -> None:  # AC2 — log=None must be safe
    _set_chat_target(conn, CHAT, "ru")
    vocab.add_word(conn, CHAT, "apple")

    counts = vocab.backfill_translations(
        conn,
        translate_fn=lambda t, target: (_ for _ in ()).throw(RuntimeError("x")),
    )

    assert counts == {"translated": 0, "failed": 1}


def test_backfill_returns_zero_for_empty_words(conn: sqlite3.Connection) -> None:  # edge: empty words table
    counts = vocab.backfill_translations(
        conn, translate_fn=lambda text, target: "should-not-be-called"
    )
    assert counts == {"translated": 0, "failed": 0}


def test_backfill_no_op_when_all_translated(conn: sqlite3.Connection) -> None:  # edge: every row already populated
    _set_chat_target(conn, CHAT, "ru")
    vocab.add_word(conn, CHAT, "apple")
    vocab.set_translation(conn, CHAT, "apple", "preset")

    calls: list[tuple[str, str]] = []

    counts = vocab.backfill_translations(
        conn,
        translate_fn=lambda text, target: calls.append((text, target)) or "x",
    )

    assert counts == {"translated": 0, "failed": 0}
    assert calls == [], f"translate_fn must not be called; got {calls}"
    assert _translation_for(conn, CHAT, "apple") == "preset"


# --- add_words_bulk translations parameter (issue #63) ---------------------


def test_add_words_bulk_default_translations_are_null(
    conn: sqlite3.Connection,
) -> None:  # AC4 — translations=None leaves all NULL (existing call sites)
    counts = vocab.add_words_bulk(conn, CHAT, ["apple", "banana"])
    assert counts == {"added": 2, "skipped": 0, "invalid": 0}
    assert _translation_for(conn, CHAT, "apple") is None
    assert _translation_for(conn, CHAT, "banana") is None


def test_add_words_bulk_with_translations_stores_each(
    conn: sqlite3.Connection,
) -> None:  # AC4 — parallel translations attach to new rows
    counts = vocab.add_words_bulk(
        conn,
        CHAT,
        ["apple", "banana"],
        translations=["яблоко", "банан"],
    )
    assert counts == {"added": 2, "skipped": 0, "invalid": 0}
    assert _translation_for(conn, CHAT, "apple") == "яблоко"
    assert _translation_for(conn, CHAT, "banana") == "банан"


def test_add_words_bulk_translations_length_mismatch_raises(
    conn: sqlite3.Connection,
) -> None:  # AC4 — error: ValueError on len mismatch
    with pytest.raises(ValueError):
        vocab.add_words_bulk(
            conn, CHAT, ["apple", "banana"], translations=["only-one"]
        )


def test_add_words_bulk_drops_translation_for_within_batch_dup(
    conn: sqlite3.Connection,
) -> None:  # AC4 — edge: in-batch duplicate's translation is discarded
    counts = vocab.add_words_bulk(
        conn,
        CHAT,
        ["apple", "apple", "banana"],
        translations=["t1", "t2", "t3"],
    )
    assert counts == {"added": 2, "skipped": 1, "invalid": 0}, f"got {counts}"
    # First "apple" wins; "t2" must not overwrite "t1".
    assert _translation_for(conn, CHAT, "apple") == "t1"
    assert _translation_for(conn, CHAT, "banana") == "t3"


def test_add_words_bulk_empty_with_empty_translations(
    conn: sqlite3.Connection,
) -> None:  # AC4 — edge: empty input + empty translations
    counts = vocab.add_words_bulk(conn, CHAT, [], translations=[])
    assert counts == {"added": 0, "skipped": 0, "invalid": 0}


# --- CSV translation round-trip (issue #64) --------------------------------


def test_format_csv_alphabetizes_with_mixed_translations() -> None:  # AC1 — header, alphabetized, None → empty cell never "None"
    out = vocab.format_csv(
        [("banana", "банан"), ("apple", None), ("Cherry", "вишня")]
    )
    assert out == "text,translation\napple,\nbanana,банан\nCherry,вишня\n", (
        f"unexpected output: {out!r}"
    )
    assert ",None" not in out, "None translation must serialize as empty cell, not literal 'None'"


def test_parse_csv_words_two_column_with_translations() -> None:  # AC2-two-col
    out = vocab.parse_csv_words("text,translation\napple,яблоко\nbanana,банан\n")
    assert out == [("apple", "яблоко"), ("banana", "банан")], f"got {out!r}"


def test_parse_csv_words_two_column_empty_second_cell() -> None:  # AC2-two-col + edge: empty 2nd cell → None
    out = vocab.parse_csv_words("text,translation\napple,яблоко\nbanana,\n")
    assert out == [("apple", "яблоко"), ("banana", None)], f"got {out!r}"


def test_parse_csv_words_two_column_header_case_insensitive() -> None:  # AC2-two-col-case
    out = vocab.parse_csv_words("TEXT,Translation\napple,яблоко\n")
    assert out == [("apple", "яблоко")], f"got {out!r}"


def test_add_words_bulk_after_parse_two_col_stores_translations(
    conn: sqlite3.Connection,
) -> None:  # AC2-bulk — parse → bulk-insert preserves translations and Nones
    pairs = vocab.parse_csv_words(
        "text,translation\napple,яблоко\nbanana,\ncherry,вишня\n"
    )
    words = [text for text, _ in pairs]
    translations = [tr for _, tr in pairs]
    counts = vocab.add_words_bulk(conn, CHAT, words, translations=translations)
    assert counts == {"added": 3, "skipped": 0, "invalid": 0}, f"got {counts}"
    assert _translation_for(conn, CHAT, "apple") == "яблоко"
    assert _translation_for(conn, CHAT, "banana") is None, (
        "empty 2nd cell must land as NULL, not empty string"
    )
    assert _translation_for(conn, CHAT, "cherry") == "вишня"


def test_parse_csv_words_two_column_skips_blank_first_cell() -> None:  # edge: empty first cell in two-col → row skipped
    text = "text,translation\napple,яблоко\n,orphan\nbanana,банан\n"
    out = vocab.parse_csv_words(text)
    assert out == [("apple", "яблоко"), ("banana", "банан")], (
        f"row with blank first cell must be dropped; got {out!r}"
    )


def test_parse_csv_words_header_only_two_column_returns_legacy_keep_header() -> None:  # edge: header-only two-col input
    # Two-column mode requires header + at least one data row. With only the header,
    # the spec resolves this to legacy single-row keep-header behaviour: the row is
    # treated as data and the first column is returned.
    out = vocab.parse_csv_words("text,translation\n")
    assert out == [("text", None)], f"got {out!r}"


def test_parse_csv_words_handles_crlf_line_endings() -> None:  # edge: \r\n parses identically to \n
    crlf = "text,translation\r\napple,яблоко\r\nbanana,банан\r\n"
    lf = "text,translation\napple,яблоко\nbanana,банан\n"
    assert vocab.parse_csv_words(crlf) == vocab.parse_csv_words(lf)
    assert vocab.parse_csv_words(crlf) == [("apple", "яблоко"), ("banana", "банан")]


def test_import_keeps_translation_null_when_csv_lacks_it(
    conn: sqlite3.Connection,
) -> None:  # AC3 — None translation survives import as NULL; no mid-import translator call
    # Legacy CSV (single column): every translation parses to None.
    legacy_pairs = vocab.parse_csv_words("apple\nbanana\n")
    assert all(tr is None for _, tr in legacy_pairs), f"legacy parse must yield None translations; got {legacy_pairs!r}"

    # Two-column CSV with empty second cells: still None.
    twocol_pairs = vocab.parse_csv_words("text,translation\ncherry,\ndurian,\n")
    assert all(tr is None for _, tr in twocol_pairs), f"empty 2nd cells must yield None; got {twocol_pairs!r}"

    pairs = legacy_pairs + twocol_pairs
    words = [text for text, _ in pairs]
    translations = [tr for _, tr in pairs]
    vocab.add_words_bulk(conn, CHAT, words, translations=translations)

    # Each row's translation column is NULL — backfill (out of scope here) will fill them.
    for w in ("apple", "banana", "cherry", "durian"):
        assert _translation_for(conn, CHAT, w) is None, (
            f"{w!r} should have NULL translation after import (no mid-import translate calls)"
        )


def test_csv_round_trip_preserves_translations_through_db(
    conn: sqlite3.Connection,
) -> None:  # AC4 — export → fresh-chat import → re-export bit-for-bit
    source = [("Apple", "яблоко"), ("banana", None), ("Cherry", "вишня")]
    serialized = vocab.format_csv(source)

    # Import into a fresh chat.
    fresh_chat = CHAT + 9001
    pairs = vocab.parse_csv_words(serialized)
    words = [text for text, _ in pairs]
    translations = [tr for _, tr in pairs]
    vocab.add_words_bulk(conn, fresh_chat, words, translations=translations)

    # Re-export from DB.
    rows = conn.execute(
        "SELECT text, translation FROM words WHERE chat_id = ?", (fresh_chat,)
    ).fetchall()
    redumped = vocab.format_csv([(r["text"], r["translation"]) for r in rows])

    # Spec: round-trip preserves the (text_lowercased, translation) set bit-for-bit.
    expected = vocab.format_csv(
        [(text.lower(), tr) for text, tr in source]
    )
    assert redumped == expected, (
        f"round-trip mismatch:\nexpected:\n{expected!r}\nactual:\n{redumped!r}"
    )


def test_csv_round_trip_unicode_preserves_translations() -> None:  # edge: unicode text + translations round-trip
    rows = [("café", "кофейня"), ("naïve", None), ("Zürich", "Цюрих")]
    serialized = vocab.format_csv(rows)
    parsed = vocab.parse_csv_words(serialized)
    assert sorted(parsed) == sorted(rows), (
        f"unicode round-trip lost data:\nin:  {rows!r}\nout: {parsed!r}"
    )


def test_format_csv_with_non_tuple_row_raises() -> None:  # error: non-2-tuple → propagates Python's natural unpacking error
    with pytest.raises((TypeError, ValueError)):
        vocab.format_csv(["apple"])  # type: ignore[list-item]
