"""Tests for the bulk-labelling helpers behind scripts/labels_cli.py."""

from __future__ import annotations

import sqlite3

import vocab


CHAT = 920


def _attach(conn: sqlite3.Connection, word: str, name: str) -> None:
    word_id = vocab.find_word_id(conn, CHAT, word)
    label_id = vocab.get_or_create_label(conn, CHAT, name)
    vocab.attach_label(conn, word_id, label_id)


# --- dump_labelling_state ------------------------------------------------------


def test_dump_lists_only_unlabelled_words_with_translation(
    conn: sqlite3.Connection,
) -> None:
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, "horse")
    vocab.add_word(conn, CHAT, "ankle")
    vocab.set_translation(conn, CHAT, "ankle", "лодыжка")
    _attach(conn, "horse", "pos:noun")

    out = vocab.dump_labelling_state(conn, CHAT)
    assert out["chat_id"] == CHAT
    assert out["words_total"] == 2
    assert out["unlabelled"] == [{"word": "ankle", "translation": "лодыжка"}]


def test_dump_taxonomy_has_counts_examples_and_no_reserved(
    conn: sqlite3.Connection,
) -> None:
    vocab.ensure_chat(conn, CHAT)
    for w in ("horse", "cow"):
        vocab.add_word(conn, CHAT, w)
        _attach(conn, w, "type:animal")
    _attach(conn, "horse", vocab.REMEMBERED_LABEL)

    out = vocab.dump_labelling_state(conn, CHAT)
    names = [t["name"] for t in out["taxonomy"]]
    assert names == ["type:animal"], "reserved system labels must be excluded"
    entry = out["taxonomy"][0]
    assert entry["count"] == 2
    assert entry["examples"] == ["cow", "horse"]


def test_dump_empty_chat(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    out = vocab.dump_labelling_state(conn, CHAT)
    assert out["unlabelled"] == [] and out["taxonomy"] == []


# --- apply_label_mapping ---------------------------------------------------------


def test_apply_attaches_labels_and_reports(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, "horse")

    out = vocab.apply_label_mapping(
        conn, CHAT, [("horse", ["pos:noun", "type:animal"])]
    )
    assert out["applied"] == 1
    assert out["attached"] == 2
    assert out["unchanged"] == 0
    assert vocab.labels_for_word(
        conn, vocab.find_word_id(conn, CHAT, "horse")
    ) == ["pos:noun", "type:animal"]


def test_apply_never_inserts_unknown_words(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    out = vocab.apply_label_mapping(conn, CHAT, [("typo-word", ["pos:noun"])])
    assert out["unknown_words"] == ["typo-word"]
    assert vocab.count_words(conn, CHAT) == 0, (
        "an agent typo must not pollute the vocab"
    )


def test_apply_is_idempotent(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, "horse")
    items = [("horse", ["pos:noun"])]
    vocab.apply_label_mapping(conn, CHAT, items)
    out = vocab.apply_label_mapping(conn, CHAT, items)
    assert out["applied"] == 0
    assert out["unchanged"] == 1


def test_apply_replaces_differing_pos(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, "run")
    _attach(conn, "run", "pos:noun")

    out = vocab.apply_label_mapping(conn, CHAT, [("run", ["pos:verb"])])
    assert out["applied"] == 1
    assert vocab.labels_for_word(
        conn, vocab.find_word_id(conn, CHAT, "run")
    ) == ["pos:verb"], "attach_label's one-POS rule must replace the old pos:*"


def test_apply_rejects_malformed_multi_pos_and_reserved(
    conn: sqlite3.Connection,
) -> None:
    vocab.ensure_chat(conn, CHAT)
    for w in ("a", "b", "c"):
        vocab.add_word(conn, CHAT, w)

    out = vocab.apply_label_mapping(
        conn,
        CHAT,
        [
            ("a", [":bad"]),
            ("b", ["pos:noun", "pos:verb"]),
            ("c", [vocab.REMEMBERED_LABEL]),
        ],
    )
    assert out["applied"] == 0
    assert len(out["rejected"]) == 3
    words = [w for w, _ in out["rejected"]]
    assert words == ["a", "b", "c"]
    for w in ("a", "b", "c"):
        assert vocab.labels_for_word(conn, vocab.find_word_id(conn, CHAT, w)) == []


def test_apply_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, "horse")

    out = vocab.apply_label_mapping(
        conn, CHAT, [("horse", ["pos:noun", "type:animal"])], dry_run=True
    )
    assert out["applied"] == 1 and out["attached"] == 2
    assert vocab.labels_for_word(
        conn, vocab.find_word_id(conn, CHAT, "horse")
    ) == []
    assert vocab.labels_with_counts(conn, CHAT) == [], (
        "dry run must not even create the label rows"
    )


def test_apply_word_lookup_is_case_insensitive(conn: sqlite3.Connection) -> None:
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, "horse")
    out = vocab.apply_label_mapping(conn, CHAT, [("Horse", ["pos:noun"])])
    assert out["applied"] == 1 and out["unknown_words"] == []
