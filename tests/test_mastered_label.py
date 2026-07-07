"""Tests for issue #141 — mastered label after 2 consecutive correct repeats.

A correct answer in a Typed-drill mastery check (a salted judged round,
source="repeat") bumps a new `words.repeat_correct_streak` integer by 1; a
wrong repeat resets it to 0. When the streak reaches `MASTERED_THRESHOLD`
(2) on a correct repeat, the `mastered` system label is auto-attached.
Mastered words are excluded from the drill's salt (mastery-check) pool.
`mastered` joins `RESERVED_LABEL_NAMES`, so `/label` and `/unlabel` reject
it the same way they reject `remembered`.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Mocking shape mirrors `tests/test_focus_hard_label.py` —
temp-DB `conn` fixture from `tests/conftest.py`, Telegram Update/Context
mocked at the architectural seam, `bot.conn` patched.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import db as db_mod  # noqa: E402
import vocab  # noqa: E402


CHAT = 1410


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


def _mark_mastered(conn: sqlite3.Connection, word: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    label_id = vocab.get_or_create_label(conn, chat, vocab.MASTERED_LABEL)
    vocab.attach_label(conn, wid, label_id)


def _mark_remembered(conn: sqlite3.Connection, word: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    label_id = vocab.get_or_create_label(conn, chat, vocab.REMEMBERED_LABEL)
    vocab.attach_label(conn, wid, label_id)


def _repeat_streak(conn: sqlite3.Connection, word_id: int) -> int:
    return conn.execute(
        "SELECT repeat_correct_streak FROM words WHERE id = ?", (word_id,)
    ).fetchone()["repeat_correct_streak"]


def _word_label_count(
    conn: sqlite3.Connection, word_id: int, label_name: str
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM word_labels wl "
        "JOIN labels l ON l.id = wl.label_id "
        "WHERE wl.word_id = ? AND l.name = ?",
        (word_id, label_name),
    ).fetchone()
    return row["n"]


def _word_columns(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("PRAGMA table_info(words)").fetchall()
    return {r["name"]: {"type": r["type"], "notnull": r["notnull"]} for r in rows}


# -- Telegram factories ------------------------------------------------------


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
    return ctx


def _patch_bot(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()
    bot.typed_drills.clear()
    if hasattr(bot, "irregulars"):
        bot.irregulars.clear()
    if hasattr(bot, "pending_game_filters"):
        bot.pending_game_filters.clear()


def _last_reply(reply_text_mock: MagicMock) -> str:
    calls = reply_text_mock.call_args_list
    assert calls, "expected at least one reply_text call, got none"
    last = calls[-1]
    if last.args:
        return last.args[0]
    return last.kwargs.get("text", "")


# === module surface =========================================================


def test_mastered_label_constant() -> None:
    # spec surface — MASTERED_LABEL must be the literal "mastered"
    assert vocab.MASTERED_LABEL == "mastered", (
        f"MASTERED_LABEL must be the literal 'mastered'; got "
        f"{vocab.MASTERED_LABEL!r}"
    )


def test_mastered_threshold_constant() -> None:
    # spec surface — MASTERED_THRESHOLD must be the int 2
    assert vocab.MASTERED_THRESHOLD == 2, (
        f"MASTERED_THRESHOLD must be the int 2; got {vocab.MASTERED_THRESHOLD!r}"
    )
    assert isinstance(vocab.MASTERED_THRESHOLD, int), (
        f"MASTERED_THRESHOLD must be an int; got "
        f"{type(vocab.MASTERED_THRESHOLD).__name__}"
    )


def test_reserved_label_names_contains_mastered() -> None:  # AC5
    # AC5 — `mastered` joins RESERVED_LABEL_NAMES so `/label` / `/unlabel`
    # reject it via the existing reserved-name guard.
    assert vocab.MASTERED_LABEL in vocab.RESERVED_LABEL_NAMES, (
        f"AC5 — RESERVED_LABEL_NAMES must contain {vocab.MASTERED_LABEL!r}; "
        f"got {vocab.RESERVED_LABEL_NAMES!r}"
    )


# === AC1 — correct/repeat increments repeat_correct_streak by 1 =============


def test_record_outcome_correct_repeat_bumps_streak(
    conn: sqlite3.Connection,
) -> None:  # AC1 — correct/repeat increments repeat_correct_streak by exactly 1
    word_id = _add_word(conn)
    assert _repeat_streak(conn, word_id) == 0, (
        "precondition: fresh word starts at repeat_correct_streak=0"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="repeat")

    assert _repeat_streak(conn, word_id) == 1, (
        f"AC1 — correct/repeat must increment repeat_correct_streak by 1; "
        f"got {_repeat_streak(conn, word_id)}"
    )


def test_record_outcome_correct_repeat_independent_of_weight(
    conn: sqlite3.Connection,
) -> None:  # AC1 — the +1 is independent of `weight`
    word_id = _add_word(conn)

    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="repeat")
    assert _repeat_streak(conn, word_id) == 1, (
        f"AC1 — weight=0.5 must still produce +1; got {_repeat_streak(conn, word_id)}"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=42.0, source="repeat")
    assert _repeat_streak(conn, word_id) == 2, (
        f"AC1 — weight=42.0 must still produce +1 (total 2); got "
        f"{_repeat_streak(conn, word_id)}"
    )


# === AC2 — wrong/repeat resets repeat_correct_streak to 0 ===================


def test_record_outcome_wrong_repeat_resets_streak(
    conn: sqlite3.Connection,
) -> None:  # AC2 — wrong/repeat resets repeat_correct_streak to 0 from nonzero
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 7 WHERE id = ?", (word_id,)
    )
    assert _repeat_streak(conn, word_id) == 7, "precondition: streak primed to 7"

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="repeat")

    assert _repeat_streak(conn, word_id) == 0, (
        f"AC2 — wrong/repeat must reset repeat_correct_streak to 0; "
        f"got {_repeat_streak(conn, word_id)}"
    )


# === AC3 — threshold-crossing attaches mastered =============================


def test_record_outcome_threshold_attaches_mastered(
    conn: sqlite3.Connection,
) -> None:  # AC3 — reaching MASTERED_THRESHOLD on a correct repeat attaches `mastered`
    word_id = _add_word(conn)
    assert vocab.MASTERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        "precondition: word must NOT carry `mastered`"
    )

    # First correct repeat: streak → 1, still below threshold (2).
    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="repeat")
    assert vocab.MASTERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        f"AC3 — streak below threshold must NOT attach `mastered`; got "
        f"{vocab.labels_for_word(conn, word_id)!r}"
    )

    # Second correct repeat: streak hits MASTERED_THRESHOLD=2 → attach.
    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="repeat")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.MASTERED_LABEL in labels, (
        f"AC3 — reaching MASTERED_THRESHOLD on a correct repeat must attach "
        f"`mastered`; got {labels!r}"
    )
    assert _word_label_count(conn, word_id, vocab.MASTERED_LABEL) == 1, (
        f"AC3 — exactly one `mastered` row must exist; got "
        f"{_word_label_count(conn, word_id, vocab.MASTERED_LABEL)}"
    )


def test_record_outcome_above_threshold_idempotent(
    conn: sqlite3.Connection,
) -> None:  # AC3 + edge — further correct repeats keep exactly one `mastered` row
    word_id = _add_word(conn)
    # Drive the streak from 0 → 2 → 3 → 4 with three correct repeats.
    for _ in range(4):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="repeat"
        )

    assert _repeat_streak(conn, word_id) == 4, (
        f"precondition: 4 correct repeats must yield streak=4; got "
        f"{_repeat_streak(conn, word_id)}"
    )
    assert _word_label_count(conn, word_id, vocab.MASTERED_LABEL) == 1, (
        f"AC3 — repeated correct repeats above threshold must keep exactly "
        f"one `mastered` row; got "
        f"{_word_label_count(conn, word_id, vocab.MASTERED_LABEL)}"
    )


# === AC4 — mastered_word_ids ================================================


def test_mastered_word_ids_empty_when_none(
    conn: sqlite3.Connection,
) -> None:  # AC4 — chat with no `mastered` words returns set()
    _add_word(conn, "alpha")
    _add_word(conn, "beta")

    result = vocab.mastered_word_ids(conn, CHAT)

    assert result == set(), (
        f"AC4 — chat with no `mastered` words must yield set(); got {result!r}"
    )
    assert isinstance(result, set), (
        f"AC4 — return type must be `set`; got {type(result).__name__}"
    )


def test_mastered_word_ids_returns_expected_ids(
    conn: sqlite3.Connection,
) -> None:  # AC4 — returns exactly the ids carrying `mastered`
    _add_word(conn, "alpha")
    _add_word(conn, "beta")
    _add_word(conn, "gamma")
    _mark_mastered(conn, "alpha")
    _mark_mastered(conn, "gamma")
    expected = {_word_id(conn, "alpha"), _word_id(conn, "gamma")}

    result = vocab.mastered_word_ids(conn, CHAT)

    assert result == expected, (
        f"AC4 — must return exactly the `mastered` word ids; got {result!r}, "
        f"expected {expected!r}"
    )


def test_mastered_word_ids_scopes_to_chat(
    conn: sqlite3.Connection,
) -> None:  # AC4 — `mastered` words in another chat must not leak
    other = CHAT + 1
    _add_word(conn, "alpha")
    _add_word(conn, "alpha", chat=other)
    _mark_mastered(conn, "alpha", chat=other)

    assert vocab.mastered_word_ids(conn, CHAT) == set(), (
        "AC4 — `mastered` words in another chat must not leak into this chat"
    )
    assert vocab.mastered_word_ids(conn, other) == {
        _word_id(conn, "alpha", chat=other)
    }, "AC4 — the other chat's mastered word must still be returned for it"


def test_mastered_word_ids_returns_when_remembered_absent(
    conn: sqlite3.Connection,
) -> None:  # AC4 + edge — mastered word that loses `remembered` is still returned
    word_id = _add_word(conn, "alpha")
    _mark_mastered(conn, "alpha")
    # Simulate DB tampering: ensure `remembered` is not attached.
    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        "precondition: word must NOT carry `remembered`"
    )

    result = vocab.mastered_word_ids(conn, CHAT)

    assert word_id in result, (
        f"AC4 — `mastered` membership must be returned regardless of "
        f"`remembered` presence; got {result!r}"
    )


# === AC5 — /label and /unlabel reject mastered ==============================


def test_cmd_label_rejects_mastered(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — /label X mastered → ⚠️, no row inserted
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()

    asyncio.run(bot.cmd_label(update, _make_context(["horse", "mastered"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC5 — reserved-label reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "mastered" in reply, (
        f"AC5 — reply must name the offending reserved label; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == [], (
        f"AC5 — no row may be inserted into word_labels on reserved-name "
        f"reject; got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_unlabel_rejects_mastered_attached(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC5 — /unlabel X mastered on attached row → ⚠️, row survives
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _mark_mastered(conn, "horse")
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == ["mastered"], (
        "precondition: mastered must be attached to horse"
    )

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(["horse", "mastered"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC5 — reserved-label /unlabel reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "mastered" in reply, (
        f"AC5 — reply must name the offending reserved label; got {reply!r}"
    )
    assert vocab.labels_for_word(conn, wid) == ["mastered"], (
        f"AC5 — the row must survive a manual /unlabel of a reserved label; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


# === AC6 — push / game sources leave repeat_correct_streak unchanged ========


def test_record_outcome_push_correct_no_streak_change(
    conn: sqlite3.Connection,
) -> None:  # AC6 — source="push" + correct must NOT touch repeat_correct_streak
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")

    assert _repeat_streak(conn, word_id) == 1, (
        f"AC6 — source='push' + correct must leave repeat_correct_streak "
        f"unchanged (regression guard); got {_repeat_streak(conn, word_id)}"
    )


def test_record_outcome_push_wrong_no_streak_change(
    conn: sqlite3.Connection,
) -> None:  # AC6 — source="push" + wrong must NOT touch repeat_correct_streak
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=1.0, source="push")

    assert _repeat_streak(conn, word_id) == 1, (
        f"AC6 — source='push' + wrong must leave repeat_correct_streak "
        f"unchanged (regression guard); got {_repeat_streak(conn, word_id)}"
    )


def test_record_outcome_game_correct_no_streak_change(
    conn: sqlite3.Connection,
) -> None:  # AC6 — source="game" + correct must NOT touch repeat_correct_streak
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")

    assert _repeat_streak(conn, word_id) == 1, (
        f"AC6 — source='game' + correct must leave repeat_correct_streak "
        f"unchanged (regression guard); got {_repeat_streak(conn, word_id)}"
    )


def test_record_outcome_game_wrong_no_streak_change(
    conn: sqlite3.Connection,
) -> None:  # AC6 — source="game" + wrong must NOT touch repeat_correct_streak
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET repeat_correct_streak = 1 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="game")

    assert _repeat_streak(conn, word_id) == 1, (
        f"AC6 — source='game' + wrong must leave repeat_correct_streak "
        f"unchanged (regression guard); got {_repeat_streak(conn, word_id)}"
    )


# === AC7 — gm:drill salt pool excludes mastered ==============================


def test_on_games_menu_gm_drill_salt_excludes_mastered(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC7 — gm:drill salt pool = remembered AND NOT mastered
    _patch_bot(monkeypatch, conn)
    # Seed 5 translatable in-progress words (the focus pool), plus one plain
    # `remembered` word (the only eligible mastery check) and one word that
    # is `remembered` AND `mastered` (must be excluded from the salt pool).
    # The draw is then fully determined: 5 focus rounds + 1 judged salt round.
    focus_words = ["alpha", "beta", "gamma", "delta", "epsilon"]
    words = focus_words + ["eta", "zeta"]
    vocab.ensure_chat(conn, CHAT)
    vocab.add_words_bulk(
        conn, CHAT, words, translations=[f"tr_{w}" for w in words]
    )
    _mark_remembered(conn, "eta")  # eligible mastery check
    _mark_remembered(conn, "zeta")
    _mark_mastered(conn, "zeta")  # mastered word that must be excluded

    eta_id = _word_id(conn, "eta")
    zeta_id = _word_id(conn, "zeta")
    focus_ids = {_word_id(conn, w) for w in focus_words}

    update = _make_callback_update("gm:drill")
    asyncio.run(bot.on_games_menu(update, _make_context([])))

    game = bot.typed_drills.get(CHAT)
    assert game is not None, (
        "AC7 — a viable 5-word focus pool must start a typed drill"
    )
    round_ids = {r.word_id for r in game.rounds}
    assert zeta_id not in round_ids, (
        f"AC7 — `mastered` word_id={zeta_id} must NOT appear in the drill "
        f"(excluded from the salt pool); got rounds={round_ids!r}"
    )
    judged = [r for r in game.rounds if r.judged]
    assert [r.word_id for r in judged] == [eta_id], (
        f"AC7 — the judged mastery checks must come from `remembered AND NOT "
        f"mastered` (only eta qualifies); got "
        f"{[(r.word_id, r.judged) for r in game.rounds]!r}"
    )
    assert round_ids == focus_ids | {eta_id}, (
        f"AC7 — rounds must be the 5 focus words plus the one eligible salt "
        f"word; got {round_ids!r}, expected {focus_ids | {eta_id}!r}"
    )


# === edges + error ==========================================================


def test_record_outcome_wrong_repeat_from_zero_idempotent(
    conn: sqlite3.Connection,
) -> None:  # edge — wrong/repeat on streak=0 stays 0 (no underflow)
    word_id = _add_word(conn)
    assert _repeat_streak(conn, word_id) == 0, "precondition: fresh streak=0"

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="repeat")

    assert _repeat_streak(conn, word_id) == 0, (
        f"edge — wrong/repeat on streak=0 must remain 0 (no underflow); "
        f"got {_repeat_streak(conn, word_id)}"
    )


def test_words_repeat_correct_streak_default_is_zero(
    conn: sqlite3.Connection,
) -> None:  # edge — fresh insert has repeat_correct_streak=0; column is INTEGER NOT NULL
    word_id = _add_word(conn, "fresh")

    assert _repeat_streak(conn, word_id) == 0, (
        f"fresh word must default to repeat_correct_streak=0; got "
        f"{_repeat_streak(conn, word_id)}"
    )

    cols = _word_columns(conn)
    assert "repeat_correct_streak" in cols, (
        f"expected repeat_correct_streak column, got {sorted(cols)}"
    )
    assert cols["repeat_correct_streak"]["type"].upper() == "INTEGER", (
        f"repeat_correct_streak must be INTEGER; got "
        f"{cols['repeat_correct_streak']['type']!r}"
    )
    assert cols["repeat_correct_streak"]["notnull"] == 1, (
        "repeat_correct_streak must be NOT NULL"
    )


def test_init_db_adds_repeat_correct_streak_to_legacy_db(
    tmp_path: Path,
) -> None:  # edge — legacy db (column missing) gets the column with default 0
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path, isolation_level=None)
    legacy.row_factory = sqlite3.Row
    # Hand-build a `words` row predating the repeat_correct_streak column.
    legacy.execute(
        """
        CREATE TABLE chats (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    legacy.execute(
        """
        CREATE TABLE words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            added_at TEXT NOT NULL,
            UNIQUE(chat_id, text),
            FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
        )
        """
    )
    legacy.execute(
        "INSERT INTO chats(chat_id, tz, created_at) VALUES (1, 'UTC', '2026-05-26')"
    )
    legacy.execute(
        "INSERT INTO words(chat_id, text, added_at) VALUES (1, 'legacy', '2026-05-26')"
    )
    legacy.close()

    # Re-open via the production connect+init path; migration must add the
    # column non-destructively and pre-existing rows must materialise as 0.
    c = db_mod.connect(path)
    db_mod.init_db(c)

    cols = {
        r["name"]: r for r in c.execute("PRAGMA table_info(words)").fetchall()
    }
    assert "repeat_correct_streak" in cols, (
        f"migration must add repeat_correct_streak to legacy db; got "
        f"{sorted(cols)}"
    )
    row = c.execute(
        "SELECT repeat_correct_streak FROM words WHERE text = 'legacy'"
    ).fetchone()
    assert row is not None, "pre-existing row must survive the migration"
    assert row["repeat_correct_streak"] == 0, (
        f"pre-existing rows must initialise to 0; got "
        f"{row['repeat_correct_streak']!r}"
    )

    # Idempotent re-run.
    db_mod.init_db(c)
    c.close()


def test_record_outcome_unknown_word_id_still_raises_keyerror(
    conn: sqlite3.Connection,
) -> None:  # error — unknown word_id still raises KeyError(word_id) (regression guard)
    vocab.ensure_chat(conn, CHAT)
    bogus_id = 999_999
    pre = conn.execute(
        "SELECT 1 FROM words WHERE id = ?", (bogus_id,)
    ).fetchone()
    assert pre is None, "precondition: bogus word_id must NOT exist"

    with pytest.raises(KeyError) as excinfo:
        vocab.record_outcome(
            conn, bogus_id, correct=True, weight=1.0, source="repeat"
        )
    assert excinfo.value.args[0] == bogus_id, (
        f"error — KeyError must carry the word_id; got args={excinfo.value.args!r}"
    )
