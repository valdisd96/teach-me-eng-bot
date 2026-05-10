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


def test_count_labels_returns_zero_for_unknown_chat(
    conn: sqlite3.Connection,
) -> None:  # AC1 — chat row absent → 0, must not raise
    pre = conn.execute(
        "SELECT 1 FROM chats WHERE chat_id = ?", (CHAT,)
    ).fetchone()
    assert pre is None, "precondition: chat row must NOT exist"
    assert vocab.count_labels(conn, CHAT) == 0, (
        "count_labels on a chat with no row at all must return 0, not raise"
    )


def test_count_labels_returns_zero_when_no_labels(
    conn: sqlite3.Connection,
) -> None:  # AC1 — chat row present but no labels rows → 0
    vocab.ensure_chat(conn, CHAT)
    assert vocab.count_labels(conn, CHAT) == 0, (
        f"chat with no labels rows must report 0; got {vocab.count_labels(conn, CHAT)}"
    )


def test_count_labels_counts_detached_labels(
    conn: sqlite3.Connection,
) -> None:  # AC2 — labels with 0 word_labels attachments still count
    vocab.ensure_chat(conn, CHAT)
    vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.get_or_create_label(conn, CHAT, "type:medicine")
    vocab.get_or_create_label(conn, CHAT, "lonely")
    attached_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels"
    ).fetchone()["n"]
    assert attached_rows == 0, (
        "precondition: no word_labels attachments should exist yet"
    )
    assert vocab.count_labels(conn, CHAT) == 3, (
        f"AC2 — three detached labels must count as 3; got {vocab.count_labels(conn, CHAT)}"
    )


def test_count_labels_is_per_chat(
    conn: sqlite3.Connection,
) -> None:  # AC3 — scoped to chat_id, never cross-chat
    vocab.ensure_chat(conn, CHAT)
    vocab.ensure_chat(conn, CHAT + 1)
    vocab.get_or_create_label(conn, CHAT, "alpha")
    vocab.get_or_create_label(conn, CHAT, "beta")
    vocab.get_or_create_label(conn, CHAT + 1, "gamma")
    vocab.get_or_create_label(conn, CHAT + 1, "delta")
    vocab.get_or_create_label(conn, CHAT + 1, "epsilon")

    assert vocab.count_labels(conn, CHAT) == 2, (
        f"chat {CHAT} has 2 labels; got {vocab.count_labels(conn, CHAT)}"
    )
    assert vocab.count_labels(conn, CHAT + 1) == 3, (
        f"chat {CHAT + 1} has 3 labels; got {vocab.count_labels(conn, CHAT + 1)}"
    )
    assert vocab.count_labels(conn, 9999) == 0, (
        "unknown chat must report 0 labels"
    )


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


def test_mark_target_word_case_insensitive_simple_match() -> None:  # AC1 — case-insensitive substring match wrapped in <b>, original casing preserved inside
    out = vocab.mark_target_word("It was a cat day", "cat")
    assert out == "It was a <b>cat</b> day", (
        f"AC1 — expected single <b>cat</b> wrap, got: {out!r}"
    )


def test_mark_target_word_wraps_every_occurrence_preserving_case() -> None:  # AC2 — every case-insensitive occurrence wrapped, casing preserved
    out = vocab.mark_target_word("Cats and CATS love cat naps", "cat")
    assert out == "<b>Cat</b>s and <b>CAT</b>S love <b>cat</b> naps", (
        f"AC2 — every match must be wrapped with original casing; got: {out!r}"
    )


def test_mark_target_word_no_match_returns_escaped_no_bold() -> None:  # AC3 — target not present → HTML-escaped text, no <b> tags
    out = vocab.mark_target_word("horse runs fast", "cat")
    assert out == "horse runs fast", (
        f"AC3 — no match must produce no <b> tags; got: {out!r}"
    )
    assert "<b>" not in out, "AC3 — output must contain no <b> tag at all"


def test_mark_target_word_only_target_other_vocab_unmarked() -> None:  # AC4 — only the explicit target is wrapped; incidental words stay plain (S1)
    out = vocab.mark_target_word("cat and dog play", "cat")
    assert out == "<b>cat</b> and dog play", (
        f"AC4/S1 — only 'cat' must be wrapped, 'dog' must stay plain; got: {out!r}"
    )
    assert "<b>dog</b>" not in out, (
        "AC4/S1 — the function must not bold any word other than the target"
    )


def test_mark_target_word_empty_text() -> None:  # AC5 — empty text short-circuits to ""
    assert vocab.mark_target_word("", "cat") == "", (
        "AC5 — empty text must short-circuit to empty string"
    )


def test_mark_target_word_escapes_html_outside_match() -> None:  # AC6 — non-match regions escaped; <b> tags themselves are not escaped
    out = vocab.mark_target_word("<cat> & dog", "cat")
    assert out == "&lt;<b>cat</b>&gt; &amp; dog", (
        f"AC6 — non-match regions must be HTML-escaped while <b> tags stay literal; got: {out!r}"
    )


def test_mark_target_word_strips_markdown_bold_around_target() -> None:  # AC7 — `**target**` markers stripped before marking, no literal stars in output
    out = vocab.mark_target_word("the **cat** sat", "cat")
    assert out == "the <b>cat</b> sat", (
        f"AC7 — markdown bold markers must be stripped, no '**' in output; got: {out!r}"
    )
    assert "*" not in out, "AC7 — no literal asterisk should leak into the output"


def test_mark_target_word_empty_target_returns_escaped_plain() -> None:  # AC8 — empty target → HTML-escaped text, no bolding
    assert vocab.mark_target_word("hello", "") == "hello", (
        "AC8 — empty target must return the (escaped) text with no bolding"
    )


def test_format_push_body_header_blank_line_marked_snippet() -> None:  # AC9 — header line, blank line, then marked snippet
    out = vocab.format_push_body("cat", "the cat sat")
    assert out == "\U0001F4CC <b>cat</b>\n\nthe <b>cat</b> sat", (
        f"AC9 — header `📌 <b>cat</b>` then blank line then marked body; got: {out!r}"
    )


def test_format_push_body_html_escapes_target_and_body() -> None:  # AC10 — HTML special chars in target/body are both escaped, but <b> tags stay literal
    out = vocab.format_push_body("<x>", "a <x> b")
    assert out == "\U0001F4CC <b>&lt;x&gt;</b>\n\na <b>&lt;x&gt;</b> b", (
        f"AC10 — header target and body match must both be HTML-safe; got: {out!r}"
    )


def test_format_push_body_empty_target_no_header() -> None:  # AC11 — defensive: empty target ⇒ no header, body returned as-is from mark_target_word
    out = vocab.format_push_body("", "text")
    assert out == "text", (
        f"AC11 — empty target must omit the header entirely; got: {out!r}"
    )
    assert "\U0001F4CC" not in out, "AC11 — no 📌 prefix when target is empty"


def test_push_header_prefix_constant() -> None:  # AC12 — module-level constant exposes the 📌 emoji
    assert vocab.PUSH_HEADER_PREFIX == "\U0001F4CC", (
        f"AC12 — PUSH_HEADER_PREFIX must equal '📌'; got: {vocab.PUSH_HEADER_PREFIX!r}"
    )


def test_mark_target_word_substring_inside_word() -> None:  # edge: substring match (no overlap-longest-wins logic — only one target in play)
    out = vocab.mark_target_word("Concatenate", "cat")
    assert out == "Con<b>cat</b>enate", (
        f"edge — substring inside a longer word must still be wrapped; got: {out!r}"
    )


def test_mark_target_word_phrase_target_with_spaces() -> None:  # edge: target is a multi-word phrase ("on the fly") — substring match still works
    out = vocab.mark_target_word("She did it on the fly today.", "on the fly")
    assert out == "She did it <b>on the fly</b> today.", (
        f"edge — phrase target must wrap as a single span; got: {out!r}"
    )


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
        ("apple", None, []),
        ("banana", None, []),
    ]


def test_parse_csv_words_header_detection_is_case_insensitive() -> None:
    assert vocab.parse_csv_words("TEXT\napple\n") == [("apple", None, [])]


def test_parse_csv_words_keeps_header_when_only_row() -> None:
    # A bare list of one line where that line is "text" must not be eaten as a header.
    assert vocab.parse_csv_words("text\n") == [("text", None, [])]


def test_parse_csv_words_accepts_bare_list_without_header() -> None:  # AC2-legacy-bare (issue #64)
    assert vocab.parse_csv_words("apple\nbanana\ncherry\n") == [
        ("apple", None, []),
        ("banana", None, []),
        ("cherry", None, []),
    ]


def test_parse_csv_words_skips_blank_rows_and_blank_first_cells() -> None:
    text = "apple\n\n , extra\nbanana\n"
    # row 2 is fully empty; row 3's first cell is blank (after strip).
    assert vocab.parse_csv_words(text) == [
        ("apple", None, []),
        ("banana", None, []),
    ]


def test_parse_csv_words_returns_first_column_only() -> None:
    text = "text,note\napple,fruit\nbanana,yellow\n"
    # `note` is not `translation`, so legacy mode wins and the second column
    # is dropped. Translations come back as None.
    assert vocab.parse_csv_words(text) == [
        ("apple", None, []),
        ("banana", None, []),
    ]


def test_parse_csv_words_strips_whitespace_but_preserves_case() -> None:
    # Casing is preserved here; lowercasing happens in add_words_bulk via _normalize.
    assert vocab.parse_csv_words("  Apple  \n  BANANA\n") == [
        ("Apple", None, []),
        ("BANANA", None, []),
    ]


def test_parse_csv_words_empty_input() -> None:
    assert vocab.parse_csv_words("") == []


def test_format_csv_emits_header_and_sorts_case_insensitively() -> None:
    out = vocab.format_csv(
        [("banana", None, []), ("Apple", None, []), ("cherry", None, [])]
    )
    assert out == "text,translation,labels\nApple,,\nbanana,,\ncherry,,\n"


def test_format_csv_empty_list_still_writes_header() -> None:
    assert vocab.format_csv([]) == "text,translation,labels\n"


def test_format_csv_uses_lf_line_terminator() -> None:
    out = vocab.format_csv([("apple", None, [])])
    assert "\r" not in out
    assert out.endswith("\n")


def test_format_csv_quotes_cells_with_csv_special_characters() -> None:
    # csv module must escape commas/quotes so the output round-trips cleanly.
    out = vocab.format_csv(
        [("needs, comma", None, []), ('has "quote"', None, [])]
    )
    assert vocab.parse_csv_words(out) == [
        ('has "quote"', None, []),
        ("needs, comma", None, []),
    ]


def test_csv_round_trip_preserves_words() -> None:
    rows = [
        ("apple", None, []),
        ("banana split", None, []),
        ("cherry", None, []),
    ]
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
        [
            ("banana", "банан", []),
            ("apple", None, []),
            ("Cherry", "вишня", []),
        ]
    )
    assert (
        out
        == "text,translation,labels\napple,,\nbanana,банан,\nCherry,вишня,\n"
    ), f"unexpected output: {out!r}"
    assert ",None" not in out, "None translation must serialize as empty cell, not literal 'None'"


def test_parse_csv_words_two_column_with_translations() -> None:  # AC2-two-col
    out = vocab.parse_csv_words("text,translation\napple,яблоко\nbanana,банан\n")
    assert out == [
        ("apple", "яблоко", []),
        ("banana", "банан", []),
    ], f"got {out!r}"


def test_parse_csv_words_two_column_empty_second_cell() -> None:  # AC2-two-col + edge: empty 2nd cell → None
    out = vocab.parse_csv_words("text,translation\napple,яблоко\nbanana,\n")
    assert out == [
        ("apple", "яблоко", []),
        ("banana", None, []),
    ], f"got {out!r}"


def test_parse_csv_words_two_column_header_case_insensitive() -> None:  # AC2-two-col-case
    out = vocab.parse_csv_words("TEXT,Translation\napple,яблоко\n")
    assert out == [("apple", "яблоко", [])], f"got {out!r}"


def test_add_words_bulk_after_parse_two_col_stores_translations(
    conn: sqlite3.Connection,
) -> None:  # AC2-bulk — parse → bulk-insert preserves translations and Nones
    triples = vocab.parse_csv_words(
        "text,translation\napple,яблоко\nbanana,\ncherry,вишня\n"
    )
    words = [text for text, _, _ in triples]
    translations = [tr for _, tr, _ in triples]
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
    assert out == [
        ("apple", "яблоко", []),
        ("banana", "банан", []),
    ], f"row with blank first cell must be dropped; got {out!r}"


def test_parse_csv_words_header_only_two_column_returns_legacy_keep_header() -> None:  # edge: header-only two-col input
    # Two-column mode requires header + at least one data row. With only the header,
    # the spec resolves this to legacy single-row keep-header behaviour: the row is
    # treated as data and the first column is returned.
    out = vocab.parse_csv_words("text,translation\n")
    assert out == [("text", None, [])], f"got {out!r}"


def test_parse_csv_words_handles_crlf_line_endings() -> None:  # edge: \r\n parses identically to \n
    crlf = "text,translation\r\napple,яблоко\r\nbanana,банан\r\n"
    lf = "text,translation\napple,яблоко\nbanana,банан\n"
    assert vocab.parse_csv_words(crlf) == vocab.parse_csv_words(lf)
    assert vocab.parse_csv_words(crlf) == [
        ("apple", "яблоко", []),
        ("banana", "банан", []),
    ]


def test_import_keeps_translation_null_when_csv_lacks_it(
    conn: sqlite3.Connection,
) -> None:  # AC3 — None translation survives import as NULL; no mid-import translator call
    # Legacy CSV (single column): every translation parses to None.
    legacy = vocab.parse_csv_words("apple\nbanana\n")
    assert all(tr is None for _, tr, _ in legacy), f"legacy parse must yield None translations; got {legacy!r}"

    # Two-column CSV with empty second cells: still None.
    twocol = vocab.parse_csv_words("text,translation\ncherry,\ndurian,\n")
    assert all(tr is None for _, tr, _ in twocol), f"empty 2nd cells must yield None; got {twocol!r}"

    triples = legacy + twocol
    words = [text for text, _, _ in triples]
    translations = [tr for _, tr, _ in triples]
    vocab.add_words_bulk(conn, CHAT, words, translations=translations)

    # Each row's translation column is NULL — backfill (out of scope here) will fill them.
    for w in ("apple", "banana", "cherry", "durian"):
        assert _translation_for(conn, CHAT, w) is None, (
            f"{w!r} should have NULL translation after import (no mid-import translate calls)"
        )


def test_csv_round_trip_preserves_translations_through_db(
    conn: sqlite3.Connection,
) -> None:  # AC4 — export → fresh-chat import → re-export bit-for-bit
    source = [
        ("Apple", "яблоко", []),
        ("banana", None, []),
        ("Cherry", "вишня", []),
    ]
    serialized = vocab.format_csv(source)

    # Import into a fresh chat.
    fresh_chat = CHAT + 9001
    triples = vocab.parse_csv_words(serialized)
    words = [text for text, _, _ in triples]
    translations = [tr for _, tr, _ in triples]
    vocab.add_words_bulk(conn, fresh_chat, words, translations=translations)

    # Re-export from DB.
    rows = conn.execute(
        "SELECT text, translation FROM words WHERE chat_id = ?", (fresh_chat,)
    ).fetchall()
    redumped = vocab.format_csv(
        [(r["text"], r["translation"], []) for r in rows]
    )

    # Spec: round-trip preserves the (text_lowercased, translation) set bit-for-bit.
    expected = vocab.format_csv(
        [(text.lower(), tr, []) for text, tr, _ in source]
    )
    assert redumped == expected, (
        f"round-trip mismatch:\nexpected:\n{expected!r}\nactual:\n{redumped!r}"
    )


def test_csv_round_trip_unicode_preserves_translations() -> None:  # edge: unicode text + translations round-trip
    rows = [
        ("café", "кофейня", []),
        ("naïve", None, []),
        ("Zürich", "Цюрих", []),
    ]
    serialized = vocab.format_csv(rows)
    parsed = vocab.parse_csv_words(serialized)
    assert sorted(parsed) == sorted(rows), (
        f"unicode round-trip lost data:\nin:  {rows!r}\nout: {parsed!r}"
    )


def test_format_csv_with_non_tuple_row_raises() -> None:  # error: non-2-tuple → propagates Python's natural unpacking error
    with pytest.raises((TypeError, ValueError)):
        vocab.format_csv(["apple"])  # type: ignore[list-item]


# --- labels DAO (issue #82) -------------------------------------------------


def _word_id(conn: sqlite3.Connection, chat_id: int, text: str) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat_id, text)
    ).fetchone()["id"]


def _label_rows(conn: sqlite3.Connection, chat_id: int) -> list[tuple[int, str]]:
    return [
        (r["id"], r["name"])
        for r in conn.execute(
            "SELECT id, name FROM labels WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    ]


def test_get_or_create_label_creates_new_row(conn: sqlite3.Connection) -> None:  # AC4 — returns int id, inserts row
    vocab.ensure_chat(conn, CHAT)
    lid = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    assert isinstance(lid, int), f"expected int id, got {type(lid).__name__}"
    rows = _label_rows(conn, CHAT)
    assert rows == [(lid, "pos:noun")], f"expected one labels row; got {rows!r}"


def test_get_or_create_label_is_idempotent(conn: sqlite3.Connection) -> None:  # AC4 + example — twice → same id, one row
    vocab.ensure_chat(conn, CHAT)
    first = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    second = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    assert first == second, f"ids must match across calls; got {first} vs {second}"
    assert len(_label_rows(conn, CHAT)) == 1, (
        f"second call must not insert a duplicate; got {_label_rows(conn, CHAT)!r}"
    )


def test_get_or_create_label_scoped_per_chat(conn: sqlite3.Connection) -> None:  # AC4 — same name in 2 chats → 2 distinct ids
    vocab.ensure_chat(conn, CHAT)
    vocab.ensure_chat(conn, CHAT + 1)
    a = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    b = vocab.get_or_create_label(conn, CHAT + 1, "pos:noun")
    assert a != b, f"per-chat labels must have distinct ids; got {a} == {b}"


def test_attach_label_returns_true_for_new(conn: sqlite3.Connection) -> None:  # AC4 — fresh attach returns True
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    lid = vocab.get_or_create_label(conn, CHAT, "type:medicine")
    assert vocab.attach_label(conn, wid, lid) is True
    assert vocab.labels_for_word(conn, wid) == ["type:medicine"]


def test_attach_label_double_attach_returns_false(conn: sqlite3.Connection) -> None:  # edge — double-attach → False
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    lid = vocab.get_or_create_label(conn, CHAT, "type:medicine")
    vocab.attach_label(conn, wid, lid)
    assert vocab.attach_label(conn, wid, lid) is False, (
        "second attach of the same (word, label) must report False"
    )
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels WHERE word_id = ? AND label_id = ?",
        (wid, lid),
    ).fetchone()
    assert rows["n"] == 1, f"only one row should exist; got {rows['n']}"


def test_attach_label_unknown_id_raises_keyerror(conn: sqlite3.Connection) -> None:  # error — unknown label_id → KeyError
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    with pytest.raises(KeyError):
        vocab.attach_label(conn, wid, 999_999)


def test_attach_pos_replaces_existing_pos_label(conn: sqlite3.Connection) -> None:  # AC5 + example — pos:verb after pos:noun keeps only pos:verb
    vocab.add_word(conn, CHAT, "run")
    wid = _word_id(conn, CHAT, "run")
    noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    verb = vocab.get_or_create_label(conn, CHAT, "pos:verb")

    assert vocab.attach_label(conn, wid, noun) is True
    assert vocab.attach_label(conn, wid, verb) is True

    assert vocab.labels_for_word(conn, wid) == ["pos:verb"], (
        "attaching a second pos:* must detach the prior pos:* atomically"
    )


def test_attach_pos_leaves_non_pos_labels_alone(conn: sqlite3.Connection) -> None:  # AC5 — non-pos:* labels untouched by the pos-swap
    vocab.add_word(conn, CHAT, "ibuprofen")
    wid = _word_id(conn, CHAT, "ibuprofen")
    noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    medicine = vocab.get_or_create_label(conn, CHAT, "type:medicine")
    verb = vocab.get_or_create_label(conn, CHAT, "pos:verb")

    vocab.attach_label(conn, wid, noun)
    vocab.attach_label(conn, wid, medicine)
    vocab.attach_label(conn, wid, verb)

    assert vocab.labels_for_word(conn, wid) == ["pos:verb", "type:medicine"], (
        "swap must scope to pos:* only; non-pos labels must survive"
    )


def test_attach_pos_noop_when_same_already_attached(conn: sqlite3.Connection) -> None:  # edge — attaching pos:noun again → False, no other pos:* removed
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    medicine = vocab.get_or_create_label(conn, CHAT, "type:medicine")

    vocab.attach_label(conn, wid, noun)
    vocab.attach_label(conn, wid, medicine)

    assert vocab.attach_label(conn, wid, noun) is False, (
        "re-attaching the same pos:* must report False"
    )
    assert vocab.labels_for_word(conn, wid) == ["pos:noun", "type:medicine"], (
        "re-attaching same pos:* must not strip other labels"
    )


def test_attach_pos_wipes_multiple_stray_pos_rows(conn: sqlite3.Connection) -> None:  # edge — multiple stray pos:* rows all wiped
    vocab.add_word(conn, CHAT, "lead")
    wid = _word_id(conn, CHAT, "lead")
    noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    verb = vocab.get_or_create_label(conn, CHAT, "pos:verb")
    adj = vocab.get_or_create_label(conn, CHAT, "pos:adj")
    new_pos = vocab.get_or_create_label(conn, CHAT, "pos:adverb")

    # Bypass the DAO to plant multiple stray pos:* rows (the DAO would reject this).
    for lid in (noun, verb, adj):
        conn.execute(
            "INSERT INTO word_labels(word_id, label_id) VALUES (?, ?)", (wid, lid)
        )

    assert vocab.attach_label(conn, wid, new_pos) is True
    assert vocab.labels_for_word(conn, wid) == ["pos:adverb"], (
        "all stray pos:* rows must be wiped, leaving only the new pos:* label"
    )


def test_detach_label_returns_true_when_attached(conn: sqlite3.Connection) -> None:  # AC4 — detach returns True iff a row was deleted
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    lid = vocab.get_or_create_label(conn, CHAT, "type:medicine")
    vocab.attach_label(conn, wid, lid)

    assert vocab.detach_label(conn, wid, lid) is True
    assert vocab.labels_for_word(conn, wid) == []


def test_detach_label_returns_false_when_absent(conn: sqlite3.Connection) -> None:  # edge — detach when not attached → False
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    lid = vocab.get_or_create_label(conn, CHAT, "type:medicine")
    # No attach — there is no row to delete.
    assert vocab.detach_label(conn, wid, lid) is False


def test_labels_for_word_returns_sorted_names(conn: sqlite3.Connection) -> None:  # AC4 — names sorted ascending
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    # Attach in non-alphabetic order; the call must still return them sorted.
    for name in ("type:medicine", "pos:noun", "category:fruit"):
        vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, name))

    out = vocab.labels_for_word(conn, wid)
    assert out == ["category:fruit", "pos:noun", "type:medicine"], f"got {out!r}"


def test_words_matching_labels_and_semantics(conn: sqlite3.Connection) -> None:  # AC4 + example — only words tagged with all of names
    for w in ("apple", "ibuprofen", "banana"):
        vocab.add_word(conn, CHAT, w)
    pos_noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    type_med = vocab.get_or_create_label(conn, CHAT, "type:medicine")

    # apple: pos:noun only.        ibuprofen: pos:noun + type:medicine.   banana: type:medicine only.
    vocab.attach_label(conn, _word_id(conn, CHAT, "apple"), pos_noun)
    vocab.attach_label(conn, _word_id(conn, CHAT, "ibuprofen"), pos_noun)
    vocab.attach_label(conn, _word_id(conn, CHAT, "ibuprofen"), type_med)
    vocab.attach_label(conn, _word_id(conn, CHAT, "banana"), type_med)

    rows = vocab.words_matching_labels(conn, CHAT, ["pos:noun", "type:medicine"])
    texts = [r["text"] for r in rows]
    assert texts == ["ibuprofen"], f"AND across labels must yield only ibuprofen; got {texts!r}"


def test_words_matching_labels_dedupes_names(conn: sqlite3.Connection) -> None:  # edge — duplicate names in input are deduped
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "ibuprofen")
    pos_noun = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, CHAT, "apple"), pos_noun)
    vocab.attach_label(conn, _word_id(conn, CHAT, "ibuprofen"), pos_noun)

    rows = vocab.words_matching_labels(conn, CHAT, ["pos:noun", "pos:noun", "pos:noun"])
    texts = sorted(r["text"] for r in rows)
    assert texts == ["apple", "ibuprofen"], (
        f"duplicate names must collapse — both noun-tagged words should appear once each; got {texts!r}"
    )


def test_words_matching_labels_empty_returns_all_in_list_words_order(
    conn: sqlite3.Connection,
) -> None:  # edge — empty names → every word for chat, ordered like list_words
    vocab.add_word(conn, CHAT, "old_high")
    vocab.add_word(conn, CHAT, "old_low")
    vocab.add_word(conn, CHAT, "new_low")
    vocab.add_word(conn, CHAT + 1, "other_chat_word")  # different chat — must not appear

    conn.execute("UPDATE words SET mention_count = 5 WHERE text = 'old_high'")
    conn.execute("UPDATE words SET added_at = '2020-01-01' WHERE text = 'old_low'")

    rows = vocab.words_matching_labels(conn, CHAT, [])
    matched = [r["text"] for r in rows]
    expected = [r["text"] for r in vocab.list_words(conn, CHAT)]
    assert matched == expected, (
        f"empty names must reproduce list_words ordering; got {matched!r} vs {expected!r}"
    )
    assert "other_chat_word" not in matched, (
        "rows must scope to chat_id; other chat's word leaked"
    )


# --- parse_label_spec (issue #83) ------------------------------------------


def test_parse_label_spec_keyvalue_tokens() -> None:  # AC4 — example ["pos:noun","type:medicine"]
    out = vocab.parse_label_spec(["pos:noun", "type:medicine"])
    assert out == ["pos:noun", "type:medicine"], f"got {out!r}"


def test_parse_label_spec_bare_string_token() -> None:  # AC4 — bare-string tokens accepted
    out = vocab.parse_label_spec(["medicine"])
    assert out == ["medicine"], f"got {out!r}"


def test_parse_label_spec_dedupes_case_normalized() -> None:  # AC4 — example ["medicine","Medicine"] → ["medicine"]
    out = vocab.parse_label_spec(["medicine", "Medicine"])
    assert out == ["medicine"], f"got {out!r}"


def test_parse_label_spec_strips_and_lowercases() -> None:  # AC4 — strip + lowercase per token
    out = vocab.parse_label_spec(["  Pos:Noun  ", "TYPE:Medicine"])
    assert out == ["pos:noun", "type:medicine"], f"got {out!r}"


def test_parse_label_spec_preserves_first_seen_order() -> None:  # AC4 — first-seen order across dedupes
    out = vocab.parse_label_spec(["zeta", "alpha", "ZETA", "alpha", "beta"])
    assert out == ["zeta", "alpha", "beta"], (
        f"first-seen order must be preserved; got {out!r}"
    )


def test_parse_label_spec_empty_input_returns_empty_list() -> None:  # edge — empty list
    assert vocab.parse_label_spec([]) == []


@pytest.mark.parametrize(
    "bad",
    [
        "",            # empty token
        "   ",         # whitespace-only token (strips to empty)
        "foo bar",     # internal whitespace in bare-string
        "pos: noun",   # internal whitespace inside a key:value
        ":",           # only colon
        ":foo",        # empty key
        "foo:",        # empty value
        "a:b:c",       # >1 colon
    ],
)
def test_parse_label_spec_rejects_malformed_token(bad: str) -> None:  # AC4 — every malformed shape rejects
    with pytest.raises(ValueError) as excinfo:
        vocab.parse_label_spec([bad])
    assert "malformed label spec" in str(excinfo.value), (
        f"error message should reference malformed-label-spec; got {excinfo.value!r}"
    )


def test_parse_label_spec_error_message_starts_with_prefix() -> None:  # AC4 — message starts "malformed label spec: "
    with pytest.raises(ValueError) as excinfo:
        vocab.parse_label_spec([":foo"])
    msg = str(excinfo.value)
    assert msg.startswith("malformed label spec: "), (
        f"message must start with the documented prefix; got {msg!r}"
    )


def test_parse_label_spec_error_lists_every_offender() -> None:  # AC4 — every bad token listed in the error
    with pytest.raises(ValueError) as excinfo:
        # Mix two distinct offenders along with one valid token; only bad ones must surface in the error.
        vocab.parse_label_spec(["pos:noun", ":foo", "a:b:c"])
    msg = str(excinfo.value)
    assert ":foo" in msg, f"first offender ':foo' must be in error msg; got {msg!r}"
    assert "a:b:c" in msg, f"second offender 'a:b:c' must be in error msg; got {msg!r}"


def test_parse_label_spec_mixed_valid_and_malformed_rejects_all() -> None:  # edge — mixed input → reject all (no partial pass)
    # Even one bad token → ValueError. The valid token must NOT come back as a partial result.
    with pytest.raises(ValueError):
        vocab.parse_label_spec(["pos:noun", ":foo"])


# --- find_word_id (issue #83) ----------------------------------------------


def test_find_word_id_returns_id_for_existing(conn: sqlite3.Connection) -> None:  # AC6 — match (chat_id, _normalize(text))
    vocab.add_word(conn, CHAT, "horse")
    expected = _word_id(conn, CHAT, "horse")
    assert vocab.find_word_id(conn, CHAT, "horse") == expected


def test_find_word_id_normalizes_input(conn: sqlite3.Connection) -> None:  # AC6 — strip + lowercase
    vocab.add_word(conn, CHAT, "horse")
    expected = _word_id(conn, CHAT, "horse")
    assert vocab.find_word_id(conn, CHAT, "  Horse  ") == expected, (
        "lookup should _normalize the text the same way add_word does"
    )
    assert vocab.find_word_id(conn, CHAT, "HORSE") == expected


def test_find_word_id_returns_none_for_missing(conn: sqlite3.Connection) -> None:  # AC6 — no match → None
    vocab.ensure_chat(conn, CHAT)
    assert vocab.find_word_id(conn, CHAT, "ghost") is None


def test_find_word_id_returns_none_for_empty_text(conn: sqlite3.Connection) -> None:  # AC6 — empty after _normalize → None
    vocab.ensure_chat(conn, CHAT)
    assert vocab.find_word_id(conn, CHAT, "") is None
    assert vocab.find_word_id(conn, CHAT, "   ") is None, (
        "whitespace-only text normalizes to empty and must return None"
    )


def test_find_word_id_scopes_to_chat(conn: sqlite3.Connection) -> None:  # AC6 — chat_id scopes the lookup
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT + 1, "horse")
    a = vocab.find_word_id(conn, CHAT, "horse")
    b = vocab.find_word_id(conn, CHAT + 1, "horse")
    assert a is not None and b is not None
    assert a != b, f"per-chat rows must yield distinct ids; got {a} vs {b}"
    # Lookup in a third chat returns None even though other chats have the word.
    assert vocab.find_word_id(conn, CHAT + 999, "horse") is None


# --- CSV labels round-trip (issue #87) -------------------------------------


def _word_label_count(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels wl "
        "JOIN words w ON w.id = wl.word_id WHERE w.chat_id = ?",
        (chat_id,),
    ).fetchone()["n"]


def _labels_table_count(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM labels WHERE chat_id = ?", (chat_id,)
    ).fetchone()["n"]


def test_format_csv_with_labels_joins_with_semicolon() -> None:  # AC1-export-cell — labels list joined w/ ";"
    out = vocab.format_csv(
        [("apple", "яблоко", ["pos:noun", "type:medicine"])]
    )
    assert (
        out == "text,translation,labels\napple,яблоко,pos:noun;type:medicine\n"
    ), f"got {out!r}"


def test_format_csv_empty_labels_emits_empty_third_cell() -> None:  # AC1-export-cell — empty list → empty cell, never "[]"/"None"
    out = vocab.format_csv([("banana", None, [])])
    assert out == "text,translation,labels\nbanana,,\n", f"got {out!r}"
    assert "[]" not in out, "empty labels must be empty cell, never literal '[]'"
    assert "None" not in out, "empty labels must be empty cell, never literal 'None'"


def test_format_csv_preserves_caller_label_order() -> None:  # AC1-export-cell — labels emitted in order given
    # Caller's order is honoured (sorting is the caller's responsibility, not format_csv's).
    out = vocab.format_csv(
        [("apple", None, ["zeta:last", "alpha:first", "mu:middle"])]
    )
    # Find the labels cell line and confirm the ;-join preserves the input ordering.
    body = out.splitlines()[1]
    assert body.endswith(",zeta:last;alpha:first;mu:middle"), (
        f"label order should match caller; got line {body!r}"
    )


def test_format_csv_quotes_label_cell_with_comma() -> None:  # edge — label cell with a comma is CSV-quoted
    # The csv module must quote any cell that contains a comma; round-tripping
    # back through parse_csv_words is the cleanest assertion.
    out = vocab.format_csv([("apple", None, ["weird,name", "pos:noun"])])
    parsed = vocab.parse_csv_words(out)
    assert parsed == [("apple", None, ["weird,name", "pos:noun"])], (
        f"comma in a label name must round-trip via csv quoting; got {parsed!r}"
    )


def test_parse_csv_words_three_col_header_parses_labels() -> None:  # AC2-import-with-labels — 3-col header detected, labels split on ";"
    out = vocab.parse_csv_words(
        "text,translation,labels\napple,яблоко,pos:noun;type:fruit\n"
    )
    assert out == [("apple", "яблоко", ["pos:noun", "type:fruit"])], f"got {out!r}"


def test_parse_csv_words_three_col_header_case_insensitive() -> None:  # AC2 — case-insensitive header detection
    out = vocab.parse_csv_words(
        "TEXT,Translation,LABELS\napple,яблоко,pos:noun\n"
    )
    assert out == [("apple", "яблоко", ["pos:noun"])], f"got {out!r}"


def test_parse_csv_words_three_col_empty_labels_cell() -> None:  # edge — empty labels cell → []
    out = vocab.parse_csv_words("text,translation,labels\napple,яблоко,\n")
    assert out == [("apple", "яблоко", [])], (
        f"empty labels cell must be [] (not [''], not None); got {out!r}"
    )


def test_parse_csv_words_three_col_strips_whitespace_in_labels() -> None:  # edge — "pos:noun ; type:medicine" → both stripped
    out = vocab.parse_csv_words(
        "text,translation,labels\napple,,pos:noun ; type:medicine\n"
    )
    assert out == [("apple", None, ["pos:noun", "type:medicine"])], (
        f"whitespace inside labels cell must be stripped; got {out!r}"
    )


def test_parse_csv_words_three_col_drops_trailing_empty_fragment() -> None:  # edge — "pos:noun;" → ["pos:noun"]
    out = vocab.parse_csv_words("text,translation,labels\napple,,pos:noun;\n")
    assert out == [("apple", None, ["pos:noun"])], (
        f"trailing ';' must drop empty fragment; got {out!r}"
    )


def test_parse_csv_words_three_col_lowercases_labels() -> None:  # edge — "POS:Noun" → "pos:noun"
    out = vocab.parse_csv_words(
        "text,translation,labels\napple,,POS:Noun;TYPE:Fruit\n"
    )
    assert out == [("apple", None, ["pos:noun", "type:fruit"])], (
        f"labels must be lowercased on parse; got {out!r}"
    )


def test_parse_csv_words_three_col_header_only_returns_empty() -> None:  # AC-rework-header-only — header-only 3-col → []
    out = vocab.parse_csv_words("text,translation,labels\n")
    # Spec: "parse_csv_words('text,translation,labels\\n') returns []."
    assert out == [], f"header-only 3-col must yield []; got {out!r}"


def test_parse_csv_words_three_col_handles_crlf() -> None:  # edge — CRLF parses identically to LF
    crlf = "text,translation,labels\r\napple,яблоко,pos:noun\r\n"
    lf = "text,translation,labels\napple,яблоко,pos:noun\n"
    assert vocab.parse_csv_words(crlf) == vocab.parse_csv_words(lf)
    assert vocab.parse_csv_words(crlf) == [("apple", "яблоко", ["pos:noun"])]


def test_labels_for_words_in_chat_returns_sorted_per_word(conn: sqlite3.Connection) -> None:  # API — values sorted ASC per word
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    # Attach in a deliberately non-alphabetic order; the result must come back sorted.
    for name in ("type:medicine", "pos:noun", "category:fruit"):
        vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, CHAT, name))

    out = vocab.labels_for_words_in_chat(conn, CHAT)
    assert out == {wid: ["category:fruit", "pos:noun", "type:medicine"]}, (
        f"per-word values must be sorted ASC; got {out!r}"
    )


def test_labels_for_words_in_chat_omits_words_with_no_labels(conn: sqlite3.Connection) -> None:  # API — only words with ≥1 label keyed
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT, "banana")  # no labels
    wid_apple = _word_id(conn, CHAT, "apple")
    vocab.attach_label(
        conn, wid_apple, vocab.get_or_create_label(conn, CHAT, "pos:noun")
    )

    out = vocab.labels_for_words_in_chat(conn, CHAT)
    assert out == {wid_apple: ["pos:noun"]}, (
        f"words with no labels must be absent (callers default to []); got {out!r}"
    )


def test_labels_for_words_in_chat_scopes_to_chat(conn: sqlite3.Connection) -> None:  # API — different chats don't leak
    vocab.add_word(conn, CHAT, "apple")
    vocab.add_word(conn, CHAT + 1, "apple")
    wid_a = _word_id(conn, CHAT, "apple")
    wid_b = _word_id(conn, CHAT + 1, "apple")
    vocab.attach_label(conn, wid_a, vocab.get_or_create_label(conn, CHAT, "pos:noun"))
    vocab.attach_label(
        conn, wid_b, vocab.get_or_create_label(conn, CHAT + 1, "type:medicine")
    )

    out_a = vocab.labels_for_words_in_chat(conn, CHAT)
    out_b = vocab.labels_for_words_in_chat(conn, CHAT + 1)
    assert out_a == {wid_a: ["pos:noun"]}, f"chat A leaked or lost rows; got {out_a!r}"
    assert out_b == {wid_b: ["type:medicine"]}, f"chat B leaked or lost rows; got {out_b!r}"


def test_import_rows_creates_and_attaches_labels(conn: sqlite3.Connection) -> None:  # AC2-import-with-labels — get_or_create + attach
    counts = vocab.import_rows(
        conn,
        CHAT,
        [("apple", "яблоко", ["pos:noun", "type:fruit"])],
    )
    assert counts["added"] == 1
    assert counts["rejected"] == 0
    assert counts["label_errors"] == []

    wid = _word_id(conn, CHAT, "apple")
    assert vocab.labels_for_word(conn, wid) == ["pos:noun", "type:fruit"], (
        "labels must have been created via get_or_create_label and attached"
    )


def test_import_rows_pre_existing_labels_preserved_additive(conn: sqlite3.Connection) -> None:  # AC2-import-with-labels — additive merge, no deletion
    # Seed: word with one pre-existing label.
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    vocab.attach_label(
        conn, wid, vocab.get_or_create_label(conn, CHAT, "category:fruit")
    )

    # Import the same word with a different label; pre-existing label must survive.
    vocab.import_rows(conn, CHAT, [("apple", None, ["type:fruit"])])

    after = vocab.labels_for_word(conn, wid)
    assert "category:fruit" in after, (
        f"pre-existing label must survive import (additive merge); got {after!r}"
    )
    assert "type:fruit" in after, f"new label must be attached; got {after!r}"


def test_import_rows_two_col_csv_no_label_changes(conn: sqlite3.Connection) -> None:  # AC2-import-no-labels-column — 2-col leaves label tables alone
    rows = vocab.parse_csv_words("text,translation\napple,яблоко\nbanana,банан\n")
    counts = vocab.import_rows(conn, CHAT, rows)

    assert counts["added"] == 2, f"expected 2 added; got {counts!r}"
    assert counts["rejected"] == 0, f"rejected must be 0 for label-free CSV; got {counts!r}"
    assert counts["label_errors"] == []
    assert _labels_table_count(conn, CHAT) == 0, "no labels rows should be created"
    assert _word_label_count(conn, CHAT) == 0, "no word_labels rows should be created"


def test_import_rows_legacy_one_col_no_label_changes(conn: sqlite3.Connection) -> None:  # AC5-backwards-compat — 1-col legacy import
    rows = vocab.parse_csv_words("text\napple\nbanana\n")
    counts = vocab.import_rows(conn, CHAT, rows)

    assert counts["added"] == 2, f"got {counts!r}"
    assert counts["rejected"] == 0
    assert _labels_table_count(conn, CHAT) == 0
    assert _word_label_count(conn, CHAT) == 0


def test_import_rows_no_header_bare_list_no_label_changes(conn: sqlite3.Connection) -> None:  # AC5-backwards-compat — no-header bare list
    rows = vocab.parse_csv_words("apple\nbanana\ncherry\n")
    counts = vocab.import_rows(conn, CHAT, rows)

    assert counts["added"] == 3, f"got {counts!r}"
    assert counts["rejected"] == 0
    assert _labels_table_count(conn, CHAT) == 0


def test_import_rows_existing_csv_counts_match_add_words_bulk(
    conn: sqlite3.Connection,
) -> None:  # AC2-import-no-labels-column — added/skipped/invalid identical to today
    vocab.add_word(conn, CHAT, "apple")  # pre-existing → should be skipped on import

    rows = vocab.parse_csv_words("apple\nbanana\n   \ncherry\n")
    counts = vocab.import_rows(conn, CHAT, rows)
    # apple (skipped, dup); banana (added); blank row dropped at parse, not invalid;
    # cherry (added). Mirror what add_words_bulk would have produced today.
    assert counts["added"] == 2, f"added: {counts!r}"
    assert counts["skipped"] == 1, f"skipped: {counts!r}"
    assert counts["rejected"] == 0
    assert counts["label_errors"] == []


def test_import_rows_rejects_multi_pos(conn: sqlite3.Connection) -> None:  # AC3-multi-pos-rejected — rejected++, label_errors entry, word NOT inserted
    counts = vocab.import_rows(
        conn,
        CHAT,
        [("apple", None, ["pos:noun", "pos:verb"])],
    )
    assert counts["rejected"] == 1, f"rejected count: {counts!r}"
    assert counts["added"] == 0, f"multi-pos row must NOT be inserted; got {counts!r}"
    assert len(counts["label_errors"]) == 1, f"one error expected; got {counts!r}"

    row_idx, msg = counts["label_errors"][0]
    assert row_idx == 1, f"row index must be 1-based; got {row_idx}"
    assert "pos" in msg.lower(), f"message should mention POS; got {msg!r}"

    # The word was NOT inserted.
    found = conn.execute(
        "SELECT 1 FROM words WHERE chat_id = ? AND text = 'apple'", (CHAT,)
    ).fetchone()
    assert found is None, "rejected row's word must not be in the words table"


def test_import_rows_continues_after_rejection(conn: sqlite3.Connection) -> None:  # AC3 — following rows still imported
    counts = vocab.import_rows(
        conn,
        CHAT,
        [
            ("apple", None, ["pos:noun", "pos:verb"]),  # rejected
            ("banana", None, ["type:fruit"]),  # imported normally
            ("cherry", None, []),  # imported, no labels
        ],
    )
    assert counts["rejected"] == 1, f"got {counts!r}"
    assert counts["added"] == 2, (
        f"banana + cherry must import despite apple being rejected; got {counts!r}"
    )

    # banana exists with type:fruit attached; cherry exists with no labels; apple absent.
    banana_id = vocab.find_word_id(conn, CHAT, "banana")
    cherry_id = vocab.find_word_id(conn, CHAT, "cherry")
    assert banana_id is not None, "banana must be inserted"
    assert cherry_id is not None, "cherry must be inserted"
    assert vocab.find_word_id(conn, CHAT, "apple") is None, "apple must not be inserted"
    assert vocab.labels_for_word(conn, banana_id) == ["type:fruit"]
    assert vocab.labels_for_word(conn, cherry_id) == []


@pytest.mark.parametrize("bad_label", [":noun", "pos:", "pos:a:b"])
def test_import_rows_rejects_malformed_label(
    conn: sqlite3.Connection, bad_label: str
) -> None:  # AC3-malformed-labels — :noun, pos:, pos:a:b each rejected
    counts = vocab.import_rows(
        conn,
        CHAT,
        [
            ("apple", None, [bad_label]),  # rejected
            ("banana", None, []),  # imported normally
        ],
    )
    assert counts["rejected"] == 1, f"{bad_label!r} should reject; got {counts!r}"
    assert counts["added"] == 1, (
        f"banana must still import after apple is rejected for {bad_label!r}; got {counts!r}"
    )
    assert len(counts["label_errors"]) == 1
    row_idx, msg = counts["label_errors"][0]
    assert row_idx == 1, f"row index must be 1-based; got {row_idx}"
    assert "malformed" in msg.lower(), (
        f"message should reference malformed labels; got {msg!r}"
    )

    # apple was NOT inserted; banana was.
    assert vocab.find_word_id(conn, CHAT, "apple") is None, (
        f"row with malformed label {bad_label!r} must not produce a word row"
    )
    assert vocab.find_word_id(conn, CHAT, "banana") is not None


def test_import_rows_label_error_index_is_one_based(conn: sqlite3.Connection) -> None:  # AC3 — first row's error has row_idx=1
    counts = vocab.import_rows(
        conn,
        CHAT,
        [
            ("apple", None, []),  # row 1 — clean
            ("banana", None, ["pos:noun", "pos:verb"]),  # row 2 — rejected
            ("cherry", None, [":bad"]),  # row 3 — rejected (malformed)
        ],
    )
    indices = sorted(idx for idx, _ in counts["label_errors"])
    assert indices == [2, 3], (
        f"row indices must be 1-based and reference original row position; got {indices!r}"
    )


def test_import_rows_dedupes_duplicate_labels_in_row(conn: sqlite3.Connection) -> None:  # edge — "pos:noun;pos:noun" → not rejected, attached once
    counts = vocab.import_rows(
        conn,
        CHAT,
        [("apple", None, ["pos:noun", "pos:noun"])],
    )
    # Per spec: duplicate labels in same row are deduped, NOT a multi-pos rejection.
    assert counts["rejected"] == 0, (
        f"duplicate identical labels must NOT trigger multi-pos rejection; got {counts!r}"
    )
    assert counts["added"] == 1, f"got {counts!r}"

    wid = _word_id(conn, CHAT, "apple")
    assert vocab.labels_for_word(conn, wid) == ["pos:noun"], (
        "duplicate pos:noun must collapse to one attached label"
    )


def test_import_rows_skips_existing_word_but_attaches_new_labels(
    conn: sqlite3.Connection,
) -> None:  # edge — duplicate text → skipped count, labels still attached additively
    # Seed: existing word with one label.
    vocab.add_word(conn, CHAT, "apple")
    wid = _word_id(conn, CHAT, "apple")
    vocab.attach_label(
        conn, wid, vocab.get_or_create_label(conn, CHAT, "type:fruit")
    )

    # Import the same text with a new label.
    counts = vocab.import_rows(conn, CHAT, [("apple", None, ["pos:noun"])])

    assert counts["added"] == 0, f"existing word must not re-add; got {counts!r}"
    assert counts["skipped"] == 1, f"existing word must count as skipped; got {counts!r}"
    after = vocab.labels_for_word(conn, wid)
    assert sorted(after) == ["pos:noun", "type:fruit"], (
        f"labels must be additive even when the row is skipped; got {after!r}"
    )


def test_import_rows_empty_input_zero_counts(conn: sqlite3.Connection) -> None:  # edge — [] → all-zero counts, no label_errors
    counts = vocab.import_rows(conn, CHAT, [])
    assert counts["added"] == 0
    assert counts["skipped"] == 0
    assert counts["invalid"] == 0
    assert counts["rejected"] == 0
    assert counts["label_errors"] == []


def test_import_rows_does_not_raise_on_bad_row_content(conn: sqlite3.Connection) -> None:  # error — malformed labels propagate via label_errors, not exception
    # Spec: "import_rows itself never raises on row content; it only raises on
    # programmer errors (e.g. mismatched argument types)."
    try:
        counts = vocab.import_rows(
            conn,
            CHAT,
            [
                ("apple", None, [":noun"]),
                ("banana", None, ["pos:noun", "pos:verb"]),
                ("cherry", None, ["pos:a:b"]),
            ],
        )
    except Exception as exc:  # noqa: BLE001 — spec forbids any raise here
        pytest.fail(f"import_rows must not raise on bad row content; raised {exc!r}")

    assert counts["rejected"] == 3, (
        f"all three malformed rows must surface via rejected/label_errors; got {counts!r}"
    )
    assert len(counts["label_errors"]) == 3


def test_csv_round_trip_preserves_label_set(conn: sqlite3.Connection) -> None:  # AC4 — export → wipe → import reproduces every word's label set
    # Seed chat A with three words and disjoint label sets.
    src_chat = CHAT
    vocab.add_word(conn, src_chat, "apple")
    vocab.add_word(conn, src_chat, "banana")
    vocab.add_word(conn, src_chat, "cherry")  # no labels
    wid_apple = _word_id(conn, src_chat, "apple")
    wid_banana = _word_id(conn, src_chat, "banana")
    for name in ("pos:noun", "type:fruit"):
        vocab.attach_label(
            conn, wid_apple, vocab.get_or_create_label(conn, src_chat, name)
        )
    vocab.attach_label(
        conn, wid_banana, vocab.get_or_create_label(conn, src_chat, "pos:noun")
    )

    # Export: build triples the way /export does — labels via labels_for_words_in_chat.
    label_map = vocab.labels_for_words_in_chat(conn, src_chat)
    rows = conn.execute(
        "SELECT id, text, translation FROM words WHERE chat_id = ?", (src_chat,)
    ).fetchall()
    triples = [
        (r["text"], r["translation"], label_map.get(r["id"], [])) for r in rows
    ]
    serialized = vocab.format_csv(triples)

    # Round-trip into a fresh chat (the "wipe" simulation).
    fresh_chat = src_chat + 12345
    parsed = vocab.parse_csv_words(serialized)
    counts = vocab.import_rows(conn, fresh_chat, parsed)
    assert counts["rejected"] == 0, (
        f"clean export must not reject anything on import; got {counts!r}"
    )

    # Verify each word's label set matches the source.
    expected = {
        "apple": {"pos:noun", "type:fruit"},
        "banana": {"pos:noun"},
        "cherry": set(),
    }
    for text, want in expected.items():
        wid = vocab.find_word_id(conn, fresh_chat, text)
        assert wid is not None, f"{text!r} missing after round-trip import"
        got = set(vocab.labels_for_word(conn, wid))
        assert got == want, (
            f"label set for {text!r} must round-trip exactly; want {want!r}, got {got!r}"
        )


# --- CSV labels rework: header-only short-circuit (issue #87 rework) -------


def test_parse_csv_words_three_col_header_only_crlf() -> None:  # AC-rework-header-only-crlf — CRLF header-only → []
    out = vocab.parse_csv_words("text,translation,labels\r\n")
    assert out == [], f"CRLF header-only 3-col must yield []; got {out!r}"


def test_parse_csv_words_three_col_header_only_mixed_case() -> None:  # AC-rework-header-case — case-insensitive header-only → []
    out = vocab.parse_csv_words("Text,Translation,Labels\n")
    assert out == [], f"mixed-case header-only 3-col must yield []; got {out!r}"


def test_parse_csv_words_three_col_header_only_then_blank_row() -> None:  # edge — header + whitespace-only row → []
    # Spec edge: "3-col header followed by one blank/whitespace-only row → still
    # [] (the blank-row skip already drops it before header detection runs)."
    out = vocab.parse_csv_words("text,translation,labels\n   \n")
    assert out == [], f"header + blank row must yield []; got {out!r}"


def test_csv_round_trip_empty_chat_is_lossless(conn: sqlite3.Connection) -> None:  # AC4-round-trip-empty — format_csv([]) → parse → import yields zero rows
    # Empty source: an empty chat has no words.
    serialized = vocab.format_csv([])
    parsed = vocab.parse_csv_words(serialized)
    assert parsed == [], (
        f"format_csv([]) must round-trip to []; got {parsed!r} from {serialized!r}"
    )

    fresh_chat = CHAT + 99999
    counts = vocab.import_rows(conn, fresh_chat, parsed)
    assert counts["added"] == 0, f"empty round-trip must insert 0 words; got {counts!r}"
    assert counts["skipped"] == 0, f"empty round-trip must skip 0; got {counts!r}"
    assert counts["rejected"] == 0, f"empty round-trip must reject 0; got {counts!r}"
    assert counts["label_errors"] == [], (
        f"empty round-trip must produce 0 label_errors; got {counts!r}"
    )

    # Zero words and zero word_labels rows for the fresh chat.
    word_count = conn.execute(
        "SELECT COUNT(*) AS n FROM words WHERE chat_id = ?", (fresh_chat,)
    ).fetchone()["n"]
    assert word_count == 0, f"empty round-trip must not insert any words; got {word_count}"

    wl_count = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels wl "
        "JOIN words w ON w.id = wl.word_id WHERE w.chat_id = ?",
        (fresh_chat,),
    ).fetchone()["n"]
    assert wl_count == 0, (
        f"empty round-trip must not insert any word_labels rows; got {wl_count}"
    )
