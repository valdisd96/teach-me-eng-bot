"""Tests for the new-word introduction quota.

Words with fewer than `vocab.INTRO_GRADUATION_REPS` FSRS ratings form the
introduction pool. Every `scheduler.INTRO_EVERY`-th push of a chat's local
day is an introduction slot that draws from that pool while bypassing the
/focus label filter — so a freshly `/add`-ed unlabelled word is guaranteed
exposure even under an active focus. Covered here:

- `vocab.select_intro_word` pool membership (reps cutoff, remembered
  exclusion, empty pool, deterministic sampling).
- `scheduler.is_intro_slot` cadence derived from push_log (same local day
  only, per-chat).
- `scheduler.compose_push` wiring: focus bypass + `is_intro` flag on intro
  slots, fallback to the focus-scoped pool once the intro pool is empty.
- `vocab.format_push_body` 🆕 badge.
"""

from __future__ import annotations

import asyncio
import datetime
import random
import sqlite3

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


def _log_push_at(conn: sqlite3.Connection, sent_at: str, chat_id: int = CHAT) -> None:
    conn.execute(
        "INSERT INTO push_log(chat_id, sent_at, tg_message_id, word_ids_json) "
        "VALUES (?, ?, NULL, '[]')",
        (chat_id, sent_at),
    )


# --- vocab.select_intro_word --------------------------------------------------


def test_select_intro_word_returns_only_underrated_words(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")
    vocab.add_word(conn, CHAT, "graduated")
    _set_reps(conn, "graduated", vocab.INTRO_GRADUATION_REPS)

    for seed in range(10):
        row = vocab.select_intro_word(conn, CHAT, rng=random.Random(seed), now=NOW)
        assert row is not None
        assert row["text"] == "fresh", (
            f"only reps < {vocab.INTRO_GRADUATION_REPS} words belong to the "
            f"introduction pool; got {row['text']!r}"
        )


def test_select_intro_word_excludes_remembered(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")
    vocab.add_word(conn, CHAT, "known")
    label_id = vocab.get_or_create_label(conn, CHAT, vocab.REMEMBERED_LABEL)
    vocab.attach_label(conn, _word_id(conn, "known"), label_id)

    for seed in range(10):
        row = vocab.select_intro_word(conn, CHAT, rng=random.Random(seed), now=NOW)
        assert row is not None and row["text"] == "fresh", (
            "remembered words must not resurface through introduction slots"
        )


def test_select_intro_word_none_when_pool_empty(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    assert vocab.select_intro_word(conn, CHAT, now=NOW) is None

    vocab.add_word(conn, CHAT, "graduated")
    _set_reps(conn, "graduated", vocab.INTRO_GRADUATION_REPS)
    assert vocab.select_intro_word(conn, CHAT, now=NOW) is None, (
        "a fully graduated vocab has no introduction pool"
    )


def test_select_intro_word_is_deterministic_with_seeded_rng(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    for w in ("alpha", "beta", "gamma", "delta"):
        vocab.add_word(conn, CHAT, w)

    first = vocab.select_intro_word(conn, CHAT, rng=random.Random(42), now=NOW)
    second = vocab.select_intro_word(conn, CHAT, rng=random.Random(42), now=NOW)
    assert first is not None and second is not None
    assert first["id"] == second["id"]


# --- scheduler.is_intro_slot ---------------------------------------------------


def test_intro_slot_cadence_every_third_push(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    expected = [True, False, False, True, False, False]
    for i, want in enumerate(expected):
        got = scheduler.is_intro_slot(conn, CHAT, "UTC", now=NOW)
        assert got is want, f"push #{i} of the day: expected intro={want}"
        _log_push_at(conn, "2026-07-07 12:00:00")


def test_intro_slot_ignores_previous_days_and_other_chats(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.ensure_chat(conn, CHAT + 1)
    _log_push_at(conn, "2026-07-06 18:00:00")  # yesterday (UTC)
    _log_push_at(conn, "2026-07-07 10:00:00", chat_id=CHAT + 1)  # other chat
    assert scheduler.is_intro_slot(conn, CHAT, "UTC", now=NOW) is True

    _log_push_at(conn, "2026-07-07 10:00:00")
    assert scheduler.is_intro_slot(conn, CHAT, "UTC", now=NOW) is False


def test_intro_slot_counts_from_local_midnight(conn: sqlite3.Connection) -> None:
    _seed_chat(conn, tz="Asia/Tokyo")
    # 20:00 UTC on Jul 6 is already Jul 7 05:00 in Tokyo — it counts as today.
    _log_push_at(conn, "2026-07-06 20:00:00")
    assert scheduler.is_intro_slot(conn, CHAT, "Asia/Tokyo", now=NOW) is False
    # …but it would NOT count for a UTC chat.
    assert scheduler.is_intro_slot(conn, CHAT, "UTC", now=NOW) is True


# --- scheduler.compose_push wiring ---------------------------------------------


async def _llm_echo(msgs: list[dict]) -> str:
    # Echo the target word back so compose_push never burns its retry.
    return msgs[-1]["content"]


def test_intro_slot_bypasses_focus_filter(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")  # unlabelled, reps=0
    vocab.add_word(conn, CHAT, "labelled")
    _set_reps(conn, "labelled", vocab.INTRO_GRADUATION_REPS)
    label_id = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, "labelled"), label_id)

    out = asyncio.run(
        scheduler.compose_push(
            conn, CHAT, llm_chat=_llm_echo,
            names=["pos:noun"], rng=random.Random(0), now=NOW,
        )
    )
    assert out is not None
    word_id, word, _, is_intro = out
    assert word == "fresh", (
        "the introduction slot must draw the unlabelled new word despite the "
        f"focus filter; got {word!r}"
    )
    assert is_intro is True
    assert word_id == _word_id(conn, "fresh")


def test_non_intro_slot_respects_focus_filter(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "fresh")  # unlabelled, reps=0
    vocab.add_word(conn, CHAT, "labelled")
    _set_reps(conn, "labelled", vocab.INTRO_GRADUATION_REPS)
    label_id = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, "labelled"), label_id)
    _log_push_at(conn, "2026-07-07 10:00:00")  # push #1 sent → next is not intro

    out = asyncio.run(
        scheduler.compose_push(
            conn, CHAT, llm_chat=_llm_echo,
            names=["pos:noun"], rng=random.Random(0), now=NOW,
        )
    )
    assert out is not None
    _, word, _, is_intro = out
    assert word == "labelled", "regular slots keep the focus-scoped pool"
    assert is_intro is False


def test_intro_slot_falls_back_to_focus_pool_when_intro_pool_empty(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "labelled")
    _set_reps(conn, "labelled", vocab.INTRO_GRADUATION_REPS)
    label_id = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, "labelled"), label_id)

    out = asyncio.run(
        scheduler.compose_push(
            conn, CHAT, llm_chat=_llm_echo,
            names=["pos:noun"], rng=random.Random(0), now=NOW,
        )
    )
    assert out is not None
    _, word, _, is_intro = out
    assert word == "labelled", (
        "an intro slot with an empty introduction pool must not drop the push"
    )
    assert is_intro is False


# --- vocab.format_push_body badge ----------------------------------------------


def test_format_push_body_intro_badge() -> None:
    plain = vocab.format_push_body("frog", "a frog jumped")
    badged = vocab.format_push_body("frog", "a frog jumped", intro=True)
    assert "🆕" not in plain
    assert badged.splitlines()[0].endswith("🆕"), (
        f"intro pushes must badge the header line; got {badged.splitlines()[0]!r}"
    )
    # Body is untouched — only the header changes.
    assert badged.splitlines()[2:] == plain.splitlines()[2:]
