"""Tests for issue #126 — auto-attach `remembered` system label and exclude
graduated words from pushes / multi-choice games.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. The
public surface under test:
- `vocab.REMEMBERED_LABEL`, `vocab.REMEMBERED_THRESHOLD`,
  `vocab.RESERVED_LABEL_NAMES` — module constants.
- `vocab.remembered_word_ids` — new query helper.
- `vocab.record_outcome` — same signature, now auto-attaches `remembered`
  when `remembered_streak` crosses the threshold from a correct outcome.
- `vocab.select_word` — same signature, now skips remembered rows.
- `bot.cmd_label` / `bot.cmd_unlabel` — reject reserved system labels.
- `bot._playable_rows` — skips remembered rows; downstream
  `cmd_games` / `on_games_menu` therefore see a pool that excludes them.

Mocking shape mirrors `tests/test_label_commands.py` and
`tests/test_games_label_spec.py`: temp-DB `conn` fixture, Telegram
Update/Context mocked at the architectural seam, `bot.conn` patched.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import games as games_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 5126


# -- helpers -----------------------------------------------------------------


def _add_word(conn: sqlite3.Connection, text: str = "apple", chat: int = CHAT) -> int:
    vocab.ensure_chat(conn, chat)
    vocab.add_word(conn, chat, text)
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat, text)
    ).fetchone()["id"]


def _word_id(conn: sqlite3.Connection, text: str, chat: int = CHAT) -> int:
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (chat, text)
    ).fetchone()["id"]


def _attach(conn: sqlite3.Connection, word: str, label: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    vocab.attach_label(conn, wid, vocab.get_or_create_label(conn, chat, label))


def _seed_translatable(
    conn: sqlite3.Connection, chat: int, words: list[str]
) -> None:
    if not words:
        vocab.ensure_chat(conn, chat)
        return
    vocab.add_words_bulk(
        conn, chat, words, translations=[f"tr_{w}" for w in words]
    )


def _mark_remembered(conn: sqlite3.Connection, word: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    label_id = vocab.get_or_create_label(conn, chat, vocab.REMEMBERED_LABEL)
    vocab.attach_label(conn, wid, label_id)


def _word_label_count(
    conn: sqlite3.Connection, word_id: int, label_name: str = vocab.REMEMBERED_LABEL
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels wl "
        "JOIN labels l ON l.id = wl.label_id "
        "WHERE wl.word_id = ? AND l.name = ?",
        (word_id, label_name),
    ).fetchone()
    return row["n"]


# -- Telegram update factories ----------------------------------------------


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(callback_data: str, chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_poll = AsyncMock()
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()
    if hasattr(bot, "irregulars"):
        bot.irregulars.clear()
    if hasattr(bot, "pending_game_filters"):
        bot.pending_game_filters.clear()


def _all_replies(reply_text_mock: MagicMock) -> list[str]:
    out: list[str] = []
    for call in reply_text_mock.call_args_list:
        if call.args:
            out.append(call.args[0])
        elif "text" in call.kwargs:
            out.append(call.kwargs["text"])
    return out


def _last_reply(reply_text_mock: MagicMock) -> str:
    replies = _all_replies(reply_text_mock)
    assert replies, "expected at least one reply_text call, got none"
    return replies[-1]


# === module surface =========================================================


def test_remembered_label_and_threshold_constants() -> None:
    # spec surface — REMEMBERED_LABEL, REMEMBERED_THRESHOLD literal values
    assert vocab.REMEMBERED_LABEL == "remembered", (
        f"REMEMBERED_LABEL must be the literal 'remembered'; got "
        f"{vocab.REMEMBERED_LABEL!r}"
    )
    assert vocab.REMEMBERED_THRESHOLD == 3.0, (
        f"REMEMBERED_THRESHOLD must be 3.0 per the spec; got "
        f"{vocab.REMEMBERED_THRESHOLD!r}"
    )


def test_reserved_label_names_contains_remembered() -> None:
    # AC7/AC8 — RESERVED_LABEL_NAMES is the set the CLI guard checks against
    assert isinstance(vocab.RESERVED_LABEL_NAMES, frozenset), (
        f"RESERVED_LABEL_NAMES must be a frozenset; got "
        f"{type(vocab.RESERVED_LABEL_NAMES).__name__}"
    )
    assert vocab.REMEMBERED_LABEL in vocab.RESERVED_LABEL_NAMES, (
        f"RESERVED_LABEL_NAMES must contain {vocab.REMEMBERED_LABEL!r}; "
        f"got {vocab.RESERVED_LABEL_NAMES!r}"
    )


# === AC1 — three pushes attach `remembered` =================================


def test_record_outcome_three_pushes_attaches_remembered(
    conn: sqlite3.Connection,
) -> None:  # AC1 — 3× correct push @ 1.0 → labelled; not labelled after 1 or 2
    word_id = _add_word(conn)

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")
    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC1 — after 1 push (streak=1.0), word must NOT be labelled; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")
    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC1 — after 2 pushes (streak=2.0), word must NOT be labelled; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")
    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id), (
        f"AC1 — after 3 pushes (streak=3.0 = threshold), word must be labelled; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )
    assert _word_label_count(conn, word_id) == 1, (
        f"AC1 — exactly one remembered row must exist; got "
        f"{_word_label_count(conn, word_id)}"
    )


# === AC2 — six games attach `remembered` ====================================


def test_record_outcome_six_games_attaches_remembered(
    conn: sqlite3.Connection,
) -> None:  # AC2 — 6× correct game @ 0.5 → labelled only on the 6th
    word_id = _add_word(conn)

    for i in range(5):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=0.5, source="game"
        )
        labels = vocab.labels_for_word(conn, word_id)
        assert vocab.REMEMBERED_LABEL not in labels, (
            f"AC2 — after {i + 1} games (streak={0.5 * (i + 1)}), word must NOT "
            f"be labelled; got {labels!r}"
        )

    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")
    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id), (
        f"AC2 — after 6 games (streak=3.0), word must be labelled; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )
    assert _word_label_count(conn, word_id) == 1


# === AC3 — mixed push + games attach `remembered` ===========================


def test_record_outcome_mixed_push_and_games_attaches_remembered(
    conn: sqlite3.Connection,
) -> None:  # AC3 — 1 push (1.0) + 4 games (0.5 each) = 3.0 → labelled on final call
    word_id = _add_word(conn)

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")
    for _ in range(3):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=0.5, source="game"
        )
    # Streak after 1.0 + 3×0.5 = 2.5 — still below threshold.
    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC3 — at streak 2.5 (pre-final), word must NOT yet be labelled; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")
    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id), (
        f"AC3 — the final 0.5 game lifts streak to 3.0 and must attach; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )
    assert _word_label_count(conn, word_id) == 1


# === AC4 — below-threshold streak does not attach ===========================


def test_record_outcome_below_threshold_two_pushes_not_labelled(
    conn: sqlite3.Connection,
) -> None:  # AC4 — 2 pushes (streak=2.0) leaves word unlabelled
    word_id = _add_word(conn)

    for _ in range(2):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )

    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC4 — streak=2.0 must not auto-attach; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )
    assert _word_label_count(conn, word_id) == 0


def test_record_outcome_below_threshold_five_games_not_labelled(
    conn: sqlite3.Connection,
) -> None:  # AC4 — 5 games (streak=2.5) leaves word unlabelled
    word_id = _add_word(conn)

    for _ in range(5):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=0.5, source="game"
        )

    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC4 — streak=2.5 must not auto-attach; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )


# === AC5 + edge — crossing the threshold repeatedly is idempotent ===========


def test_record_outcome_threshold_crossing_idempotent_no_dup_rows(
    conn: sqlite3.Connection,
) -> None:  # AC5 — crossing threshold a second time inserts no duplicate row
    word_id = _add_word(conn)

    # Cross the threshold once.
    for _ in range(3):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )
    assert _word_label_count(conn, word_id) == 1

    # Manually bump streak (simulates "crossing again" via more correct calls)
    # then another correct outcome to drive the auto-attach path.
    conn.execute(
        "UPDATE words SET remembered_streak = 0.0 WHERE id = ?", (word_id,)
    )
    for _ in range(3):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )

    assert _word_label_count(conn, word_id) == 1, (
        f"AC5 — second crossing must not insert a duplicate word_labels row; "
        f"got {_word_label_count(conn, word_id)} rows"
    )


def test_record_outcome_pre_existing_remembered_row_no_dup(
    conn: sqlite3.Connection,
) -> None:  # edge — pre-existing `remembered` row + threshold cross → still 1 row
    word_id = _add_word(conn)
    # Simulate a `remembered` row attached before this change shipped.
    label_id = vocab.get_or_create_label(conn, CHAT, vocab.REMEMBERED_LABEL)
    vocab.attach_label(conn, word_id, label_id)
    assert _word_label_count(conn, word_id) == 1

    for _ in range(3):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )

    assert _word_label_count(conn, word_id) == 1, (
        f"edge — auto-attach must be idempotent against an existing row; got "
        f"{_word_label_count(conn, word_id)} rows"
    )


def test_remembered_threshold_exactly_three_counts_as_crossing(
    conn: sqlite3.Connection,
) -> None:  # edge — streak exactly == 3.0 counts as crossing (>=)
    word_id = _add_word(conn)
    # Drive streak to 2.0, then exactly hit 3.0 with one more 1.0 push.
    for _ in range(2):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )
    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")

    streak = conn.execute(
        "SELECT remembered_streak FROM words WHERE id = ?", (word_id,)
    ).fetchone()["remembered_streak"]
    assert streak == 3.0, f"precondition: streak must be exactly 3.0; got {streak}"
    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id), (
        "edge — streak == REMEMBERED_THRESHOLD must trigger auto-attach (>=, not >)"
    )


# === AC6 — wrong outcome does not attach and does not detach ================


def test_record_outcome_wrong_does_not_attach(
    conn: sqlite3.Connection,
) -> None:  # AC6 — correct=False resets streak and does not attach
    word_id = _add_word(conn)
    # Prime streak just below threshold so a wrong call could plausibly "cross".
    conn.execute(
        "UPDATE words SET remembered_streak = 2.5 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=1.0, source="push")

    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC6 — wrong outcome must not attach `remembered`; "
        f"got {vocab.labels_for_word(conn, word_id)!r}"
    )
    streak = conn.execute(
        "SELECT remembered_streak FROM words WHERE id = ?", (word_id,)
    ).fetchone()["remembered_streak"]
    assert streak == 0.0, f"AC6 — wrong outcome must reset streak to 0; got {streak}"


def test_record_outcome_wrong_on_already_labelled_does_not_detach(
    conn: sqlite3.Connection,
) -> None:  # AC6 — out-of-scope guard: wrong outcome leaves existing label intact
    word_id = _add_word(conn)
    # Auto-attach via three pushes first.
    for _ in range(3):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )
    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id)

    vocab.record_outcome(conn, word_id, correct=False, weight=1.0, source="push")

    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id), (
        f"AC6 — detach-on-reset is out of scope; existing `remembered` row must "
        f"survive a wrong outcome; got {vocab.labels_for_word(conn, word_id)!r}"
    )


# === AC7 — cmd_label rejects reserved names =================================


def test_cmd_label_rejects_reserved_remembered(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — /label X remembered → ⚠️, no row inserted
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()

    asyncio.run(bot.cmd_label(update, _make_context(["horse", "remembered"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC7 — reserved-label reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "remembered" in reply, (
        f"AC7 — reply must name the offending reserved label; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == [], (
        f"AC7 — no row may be inserted into word_labels on reserved-name reject; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_mixed_reserved_and_free_rejects_whole_spec(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 + edge — mixed reserved + free names → entire spec rejected
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    # Pre-attach an unrelated label to confirm it is untouched.
    pos_id = vocab.get_or_create_label(conn, CHAT, "pos:noun")
    vocab.attach_label(conn, _word_id(conn, "horse"), pos_id)

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "type:animal", "remembered"]
    )))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC7 — mixed reserved+free spec must yield ⚠️ reply; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    labels = vocab.labels_for_word(conn, wid)
    assert "type:animal" not in labels, (
        f"AC7 — partial application forbidden; type:animal must not be attached; "
        f"got {labels!r}"
    )
    assert "remembered" not in labels, (
        f"AC7 — reserved label must not be attached; got {labels!r}"
    )
    # Pre-existing label must remain.
    assert labels == ["pos:noun"], (
        f"AC7 — existing pos:noun must remain untouched on reject; got {labels!r}"
    )


def test_cmd_label_reserved_uppercase_normalised_and_rejected(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 + edge — /label X REMEMBERED still caught after parse_label_spec lowercases
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()

    asyncio.run(bot.cmd_label(update, _make_context(["horse", "  REMEMBERED  "])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC7 — uppercase / whitespace-padded reserved name must still be caught "
        f"after parser normalisation; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == [], (
        f"AC7 — no rows after normalised reserved name rejected; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


# === AC8 — cmd_unlabel rejects reserved names ===============================


def test_cmd_unlabel_rejects_reserved_remembered(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC8 — /unlabel X remembered → ⚠️, no row deleted
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    # Pre-attach `remembered` (simulating an auto-attach from earlier) so we
    # can prove it survives a manual /unlabel attempt.
    _mark_remembered(conn, "horse")
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == ["remembered"]

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(["horse", "remembered"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC8 — reserved-label /unlabel reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "remembered" in reply, (
        f"AC8 — reply must name the offending reserved label; got {reply!r}"
    )
    assert vocab.labels_for_word(conn, wid) == ["remembered"], (
        f"AC8 — the row must survive a manual /unlabel; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_unlabel_mixed_reserved_and_free_rejects_whole_spec(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC8 — mixed spec → whole spec rejected (no partial deletes)
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    wid = _word_id(conn, "horse")
    vocab.attach_label(
        conn, wid, vocab.get_or_create_label(conn, CHAT, "type:animal")
    )
    _mark_remembered(conn, "horse")
    # Sanity: both attached now.
    assert set(vocab.labels_for_word(conn, wid)) == {"type:animal", "remembered"}

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(
        ["horse", "type:animal", "remembered"]
    )))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC8 — mixed reserved+free /unlabel spec must yield ⚠️ reply; got {reply!r}"
    )
    # Both labels must survive — no partial deletion.
    surviving = set(vocab.labels_for_word(conn, wid))
    assert surviving == {"type:animal", "remembered"}, (
        f"AC8 — no row may be deleted on reserved-name reject; got {surviving!r}"
    )


# === AC9 — select_word excludes remembered ==================================


def test_select_word_excludes_remembered_with_names_none(
    conn: sqlite3.Connection,
) -> None:  # AC9 — names=None → remembered row never returned
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    _mark_remembered(conn, "beta")

    rng = random.Random(0)
    picks = {
        vocab.select_word(conn, CHAT, rng=rng)["text"]
        for _ in range(50)
    }

    assert picks == {"alpha"}, (
        f"AC9 — only the unremembered word must ever surface (names=None); "
        f"got picks={picks!r}"
    )


def test_select_word_excludes_remembered_with_matching_label_any(
    conn: sqlite3.Connection,
) -> None:  # AC9 — mode="any" label filter that would otherwise include the remembered row
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    # Both carry pos:noun; beta is also remembered. mode="any" with names=["pos:noun"]
    # would pull beta in if the exclusion did not apply.
    _attach(conn, "alpha", "pos:noun")
    _attach(conn, "beta", "pos:noun")
    _mark_remembered(conn, "beta")

    rng = random.Random(0)
    picks = {
        vocab.select_word(
            conn, CHAT, rng=rng, names=["pos:noun"], mode="any"
        )["text"]
        for _ in range(50)
    }

    assert picks == {"alpha"}, (
        f"AC9 — mode='any' with a label that matches the remembered word must "
        f"still exclude it; got picks={picks!r}"
    )


def test_select_word_excludes_remembered_with_matching_label_all(
    conn: sqlite3.Connection,
) -> None:  # AC9 — mode="all" same: remembered must be excluded
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    _attach(conn, "alpha", "pos:noun")
    _attach(conn, "beta", "pos:noun")
    _mark_remembered(conn, "beta")

    rng = random.Random(0)
    picks = {
        vocab.select_word(
            conn, CHAT, rng=rng, names=["pos:noun"], mode="all"
        )["text"]
        for _ in range(50)
    }

    assert picks == {"alpha"}, (
        f"AC9 — mode='all' with matching label must still exclude remembered; "
        f"got picks={picks!r}"
    )


# === AC10 — select_word returns None when every word is remembered ==========


def test_select_word_returns_none_when_all_remembered(
    conn: sqlite3.Connection,
) -> None:  # AC10 + edge — empty effective pool → None
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    _mark_remembered(conn, "alpha")
    _mark_remembered(conn, "beta")

    picked = vocab.select_word(conn, CHAT, rng=random.Random(0))

    assert picked is None, (
        f"AC10 — every word remembered → effective pool is empty → must return "
        f"None; got {picked!r}"
    )


# === AC11 — _playable_rows excludes remembered ==============================


def test_playable_rows_excludes_remembered(
    conn: sqlite3.Connection,
) -> None:  # AC11 — _playable_rows skips remembered rows on the unfiltered branch
    _seed_translatable(conn, CHAT, ["alpha", "beta", "gamma"])
    _mark_remembered(conn, "beta")

    rows = bot._playable_rows(conn, CHAT)

    texts = sorted(r["text"] for r in rows)
    assert texts == ["alpha", "gamma"], (
        f"AC11 — remembered row must be excluded from the unfiltered pool; "
        f"got {texts!r}"
    )


def test_playable_rows_excludes_remembered_with_label_filter(
    conn: sqlite3.Connection,
) -> None:  # AC11 — exclusion applies on the label-filtered branch too
    _seed_translatable(conn, CHAT, ["alpha", "beta", "gamma"])
    for w in ("alpha", "beta", "gamma"):
        _attach(conn, w, "pos:noun")
    _mark_remembered(conn, "beta")

    rows = bot._playable_rows(conn, CHAT, ["pos:noun"])

    texts = sorted(r["text"] for r in rows)
    assert texts == ["alpha", "gamma"], (
        f"AC11 — remembered row must be excluded even when it would otherwise "
        f"match the label filter; got {texts!r}"
    )


def test_cmd_games_below_min_after_remembered_exclusion(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC11 — exclusion drops pool < MIN_VOCAB → on_games_menu reports GAMES_NEED_VOCAB
    _patch_bot(monkeypatch, conn)
    # Seed exactly MIN_VOCAB translatable rows, then graduate enough to bring the
    # playable pool under MIN_VOCAB. With MIN_VOCAB=4 and 3 remembered, pool=1.
    words = [f"w{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, words)
    for w in words[: games_module.MIN_VOCAB - 1]:
        _mark_remembered(conn, w)

    # Sanity: _playable_rows now under MIN_VOCAB.
    assert len(bot._playable_rows(conn, CHAT)) < games_module.MIN_VOCAB

    update = _make_callback_update("gm:wt")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    reply = _last_reply(update.callback_query.message.reply_text)
    assert reply == bot.GAMES_NEED_VOCAB, (
        f"AC11 — no stash + post-exclusion pool < MIN_VOCAB ⇒ GAMES_NEED_VOCAB "
        f"(exactly as if remembered rows did not exist); got {reply!r}"
    )
    assert CHAT not in bot.games, (
        f"AC11 — no game must be started when the effective pool is too small"
    )


def test_cmd_games_label_pool_below_min_after_remembered_exclusion(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC11 — filtered pool drops < MIN_VOCAB after exclusion → GAMES_NO_LABEL_MATCH
    _patch_bot(monkeypatch, conn)
    # Seed plenty of overall vocab; only some carry pos:noun; graduate most of them.
    matching = [f"m{i}" for i in range(games_module.MIN_VOCAB)]
    other = [f"o{i}" for i in range(games_module.MIN_VOCAB)]
    _seed_translatable(conn, CHAT, matching + other)
    for w in matching:
        _attach(conn, w, "pos:noun")
    # Remember all but one of the pos:noun matches → filtered pool size = 1.
    for w in matching[: games_module.MIN_VOCAB - 1]:
        _mark_remembered(conn, w)

    update = _make_command_update()
    asyncio.run(bot.cmd_games(update, _make_context(["pos:noun"])))

    reply = _last_reply(update.message.reply_text)
    assert reply == bot.GAMES_NO_LABEL_MATCH, (
        f"AC11 — label-filtered pool < MIN_VOCAB after remembered exclusion ⇒ "
        f"GAMES_NO_LABEL_MATCH (exactly as if remembered rows did not exist); "
        f"got {reply!r}"
    )


# === AC12 — remembered_word_ids =============================================


def test_remembered_word_ids_empty_set_for_clean_chat(
    conn: sqlite3.Connection,
) -> None:  # AC12 — no remembered words → empty set
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")

    result = vocab.remembered_word_ids(conn, CHAT)

    assert result == set(), (
        f"AC12 — chat with no remembered words must yield empty set; got {result!r}"
    )
    assert isinstance(result, set), (
        f"AC12 — return type must be `set`; got {type(result).__name__}"
    )


def test_remembered_word_ids_returns_correct_ids(
    conn: sqlite3.Connection,
) -> None:  # AC12 — multiple remembered words → exact id set
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    vocab.add_word(conn, CHAT, "gamma")
    _mark_remembered(conn, "alpha")
    _mark_remembered(conn, "gamma")
    expected = {_word_id(conn, "alpha"), _word_id(conn, "gamma")}

    result = vocab.remembered_word_ids(conn, CHAT)

    assert result == expected, (
        f"AC12 — must return exactly the remembered word ids; "
        f"got {result!r}, expected {expected!r}"
    )


def test_remembered_word_ids_unaffected_by_other_labels(
    conn: sqlite3.Connection,
) -> None:  # AC12 — non-`remembered` labels do not pollute the result
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    _attach(conn, "alpha", "pos:noun")
    _attach(conn, "beta", "type:animal")
    # No `remembered` rows.

    assert vocab.remembered_word_ids(conn, CHAT) == set(), (
        "AC12 — non-`remembered` labels must not leak into the result"
    )

    # Now add `remembered` to one of them; only that one should appear.
    _mark_remembered(conn, "alpha")
    assert vocab.remembered_word_ids(conn, CHAT) == {_word_id(conn, "alpha")}, (
        f"AC12 — only the `remembered`-tagged word must appear; "
        f"got {vocab.remembered_word_ids(conn, CHAT)!r}"
    )


def test_remembered_word_ids_scopes_to_chat(
    conn: sqlite3.Connection,
) -> None:  # AC12 — other chats sharing the label name do not leak across
    other = CHAT + 1
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, other, "alpha")  # same word text, different chat
    _mark_remembered(conn, "alpha", chat=other)

    assert vocab.remembered_word_ids(conn, CHAT) == set(), (
        "AC12 — remembered words in another chat must not leak into this chat"
    )
    assert vocab.remembered_word_ids(conn, other) == {
        _word_id(conn, "alpha", chat=other)
    }, "AC12 — the other chat's remembered word must still be returned for it"
