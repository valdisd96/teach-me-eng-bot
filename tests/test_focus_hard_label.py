"""Tests for issue #128 — forget-flip on wrong-in-repeat-game.

A wrong answer in the repeat game (source="repeat") detaches `remembered`,
attaches the new reserved system label `focus:hard`, resets the streak, and
gives the word a 2× weight boost in pushes and multi-choice games until it
re-graduates. `focus:hard` joins `RESERVED_LABEL_NAMES` so `/label` and
`/unlabel` reject it (mirrors `remembered`), but `/focus` still accepts it
in user-supplied specs.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
Mocking shape mirrors `tests/test_remembered_label.py` —
temp-DB `conn` fixture from `tests/conftest.py`, Telegram Update/Context
mocked at the architectural seam, `bot.conn` patched.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import random
import sqlite3
from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import vocab  # noqa: E402


CHAT = 1280
UTC = datetime.timezone.utc


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


def _mark_remembered(conn: sqlite3.Connection, word: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    label_id = vocab.get_or_create_label(conn, chat, vocab.REMEMBERED_LABEL)
    vocab.attach_label(conn, wid, label_id)


def _mark_focus_hard(conn: sqlite3.Connection, word: str, chat: int = CHAT) -> None:
    wid = _word_id(conn, word, chat)
    label_id = vocab.get_or_create_label(conn, chat, vocab.FOCUS_HARD_LABEL)
    vocab.attach_label(conn, wid, label_id)


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


def _streak(conn: sqlite3.Connection, word_id: int) -> float:
    return conn.execute(
        "SELECT remembered_streak FROM words WHERE id = ?", (word_id,)
    ).fetchone()["remembered_streak"]


# -- Telegram update factories ----------------------------------------------


def _make_command_update(chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.reply_text = AsyncMock()
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


def test_focus_hard_label_and_boost_constants() -> None:
    # spec surface — FOCUS_HARD_LABEL and HARD_FOCUS_BOOST literal values
    assert vocab.FOCUS_HARD_LABEL == "focus:hard", (
        f"FOCUS_HARD_LABEL must be the literal 'focus:hard'; got "
        f"{vocab.FOCUS_HARD_LABEL!r}"
    )
    assert vocab.HARD_FOCUS_BOOST == 2.0, (
        f"HARD_FOCUS_BOOST must be 2.0 per the spec; got {vocab.HARD_FOCUS_BOOST!r}"
    )


def test_reserved_label_names_contains_focus_hard() -> None:  # AC2a
    # AC2a — RESERVED_LABEL_NAMES == frozenset({"remembered", "focus:hard"})
    assert vocab.RESERVED_LABEL_NAMES == frozenset({"remembered", "focus:hard"}), (
        f"AC2a — RESERVED_LABEL_NAMES must be exactly "
        f"frozenset({{'remembered', 'focus:hard'}}); got "
        f"{vocab.RESERVED_LABEL_NAMES!r}"
    )


# === AC1 — forget-flip on wrong-in-repeat ===================================


def test_record_outcome_wrong_repeat_on_remembered_flips_labels(
    conn: sqlite3.Connection,
) -> None:  # AC1a — wrong repeat on remembered: detach remembered, attach focus:hard, streak=0
    word_id = _add_word(conn)
    _mark_remembered(conn, "apple")
    # Prime the streak above 0 so we can prove the call resets it.
    conn.execute(
        "UPDATE words SET remembered_streak = 4.0 WHERE id = ?", (word_id,)
    )
    assert vocab.REMEMBERED_LABEL in vocab.labels_for_word(conn, word_id), (
        "precondition: word must carry `remembered`"
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="repeat")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.REMEMBERED_LABEL not in labels, (
        f"AC1a — wrong repeat must detach `remembered`; got {labels!r}"
    )
    assert vocab.FOCUS_HARD_LABEL in labels, (
        f"AC1a — wrong repeat must attach `focus:hard`; got {labels!r}"
    )
    assert _streak(conn, word_id) == 0.0, (
        f"AC1a — wrong repeat must reset streak to 0.0; got {_streak(conn, word_id)}"
    )
    # Exactly one focus:hard row.
    assert _word_label_count(conn, word_id, vocab.FOCUS_HARD_LABEL) == 1, (
        f"AC1a — exactly one focus:hard row must exist; got "
        f"{_word_label_count(conn, word_id, vocab.FOCUS_HARD_LABEL)}"
    )


def test_record_outcome_wrong_repeat_no_remembered_attaches(
    conn: sqlite3.Connection,
) -> None:  # AC1b — wrong repeat on un-remembered word still attaches focus:hard, resets streak
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET remembered_streak = 1.5 WHERE id = ?", (word_id,)
    )
    assert vocab.REMEMBERED_LABEL not in vocab.labels_for_word(conn, word_id), (
        "precondition: word must NOT carry `remembered`"
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="repeat")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.FOCUS_HARD_LABEL in labels, (
        f"AC1b — wrong repeat on un-remembered word must still attach "
        f"`focus:hard`; got {labels!r}"
    )
    assert _streak(conn, word_id) == 0.0, (
        f"AC1b — streak must reset to 0 regardless; got {_streak(conn, word_id)}"
    )


def test_record_outcome_wrong_game_no_label_changes(
    conn: sqlite3.Connection,
) -> None:  # AC1c — source="game" + wrong: streak reset only, no label changes
    word_id = _add_word(conn)
    _mark_remembered(conn, "apple")
    conn.execute(
        "UPDATE words SET remembered_streak = 3.0 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="game")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.REMEMBERED_LABEL in labels, (
        f"AC1c — wrong/game must NOT detach `remembered` (regression guard); "
        f"got {labels!r}"
    )
    assert vocab.FOCUS_HARD_LABEL not in labels, (
        f"AC1c — wrong/game must NOT attach `focus:hard` (forget-flip is "
        f"repeat-only); got {labels!r}"
    )
    assert _streak(conn, word_id) == 0.0, (
        f"AC1c — wrong/game must still reset streak; got {_streak(conn, word_id)}"
    )


def test_record_outcome_wrong_push_no_label_changes(
    conn: sqlite3.Connection,
) -> None:  # AC1c — source="push" + wrong: streak reset only, no label changes
    word_id = _add_word(conn)
    _mark_remembered(conn, "apple")
    conn.execute(
        "UPDATE words SET remembered_streak = 3.0 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=1.0, source="push")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.REMEMBERED_LABEL in labels, (
        f"AC1c — wrong/push must NOT detach `remembered` (regression guard); "
        f"got {labels!r}"
    )
    assert vocab.FOCUS_HARD_LABEL not in labels, (
        f"AC1c — wrong/push must NOT attach `focus:hard` (forget-flip is "
        f"repeat-only); got {labels!r}"
    )


# === AC2 — /label and /unlabel reject focus:hard ============================


def test_cmd_label_rejects_focus_hard(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2b — /label X focus:hard → ⚠️, no row inserted
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()

    asyncio.run(bot.cmd_label(update, _make_context(["horse", "focus:hard"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC2b — reserved-label reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "focus:hard" in reply, (
        f"AC2b — reply must name the offending reserved label; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == [], (
        f"AC2b — no row may be inserted into word_labels on reserved-name reject; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_focus_hard_uppercase_whitespace_rejected(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2b + edge whitespace/uppercase — `/label X  FOCUS:HARD ` still rejected after parse_label_spec normalisation
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    update = _make_command_update()

    asyncio.run(bot.cmd_label(update, _make_context(["horse", "  FOCUS:HARD  "])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC2b — uppercase / whitespace-padded reserved name must still be "
        f"caught after parser normalisation; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == [], (
        f"AC2b — no rows after normalised reserved name rejected; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


def test_cmd_label_mixed_focus_hard_rejects_whole_spec(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # edge mixed reserved+free — /label horse type:animal focus:hard → whole spec rejected
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")

    update = _make_command_update()
    asyncio.run(bot.cmd_label(update, _make_context(
        ["horse", "type:animal", "focus:hard"]
    )))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"edge — mixed reserved+free spec must yield ⚠️ reply; got {reply!r}"
    )
    wid = _word_id(conn, "horse")
    labels = set(vocab.labels_for_word(conn, wid))
    assert "type:animal" not in labels, (
        f"edge — partial application forbidden; type:animal must not be attached; "
        f"got {labels!r}"
    )
    assert "focus:hard" not in labels, (
        f"edge — reserved label must not be attached; got {labels!r}"
    )


def test_cmd_unlabel_rejects_focus_hard_attached(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC2c — /unlabel X focus:hard on attached row → ⚠️, row survives
    _patch_bot(monkeypatch, conn)
    vocab.add_word(conn, CHAT, "horse")
    _mark_focus_hard(conn, "horse")
    wid = _word_id(conn, "horse")
    assert vocab.labels_for_word(conn, wid) == ["focus:hard"], (
        "precondition: focus:hard must be attached to horse"
    )

    update = _make_command_update()
    asyncio.run(bot.cmd_unlabel(update, _make_context(["horse", "focus:hard"])))

    reply = _last_reply(update.message.reply_text)
    assert reply.startswith("⚠️ "), (
        f"AC2c — reserved-label /unlabel reply must be ⚠️-prefixed; got {reply!r}"
    )
    assert "focus:hard" in reply, (
        f"AC2c — reply must name the offending reserved label; got {reply!r}"
    )
    assert vocab.labels_for_word(conn, wid) == ["focus:hard"], (
        f"AC2c — the row must survive a manual /unlabel; "
        f"got {vocab.labels_for_word(conn, wid)!r}"
    )


# === AC3 — compute_weight boost =============================================


def test_compute_weight_no_kwarg_returns_base_product(
    conn: sqlite3.Connection,
) -> None:  # AC3a — compute_weight(row, now) (no kwarg) returns unchanged base
    vocab.add_word(conn, CHAT, "fresh")
    row = conn.execute(
        "SELECT * FROM words WHERE text = 'fresh'"
    ).fetchone()
    now = datetime.datetime.now(UTC)

    base = vocab.compute_weight(row, now)

    # Sanity: a fresh unrated word has near-max weight in the existing formula
    # (forget_prob=1, recency≈1, rarity=1 → ~8.0). We assert "near 8" only as
    # a smoke check; AC3 cares about the multiplicative relationship below.
    assert 7.9 < base <= 8.0, (
        f"AC3a — fresh-word base weight must be ~8.0 (sanity); got {base}"
    )


def test_compute_weight_with_hard_word_id_doubles(
    conn: sqlite3.Connection,
) -> None:  # AC3b — compute_weight(row, now, hard_word_ids={row["id"]}) == 2.0 * base
    vocab.add_word(conn, CHAT, "fresh")
    row = conn.execute(
        "SELECT * FROM words WHERE text = 'fresh'"
    ).fetchone()
    now = datetime.datetime.now(UTC)

    base = vocab.compute_weight(row, now)
    boosted = vocab.compute_weight(row, now, hard_word_ids={row["id"]})

    assert boosted == pytest.approx(2.0 * base), (
        f"AC3b — focus:hard membership must boost weight by exactly 2.0×; "
        f"base={base}, boosted={boosted}, ratio={boosted / base if base else 'div0'}"
    )


def test_compute_weight_set_excluding_id_returns_base(
    conn: sqlite3.Connection,
) -> None:  # AC3c — empty set / set without row id → unchanged base
    vocab.add_word(conn, CHAT, "fresh")
    row = conn.execute(
        "SELECT * FROM words WHERE text = 'fresh'"
    ).fetchone()
    now = datetime.datetime.now(UTC)

    base = vocab.compute_weight(row, now)
    empty = vocab.compute_weight(row, now, hard_word_ids=set())
    other_ids = vocab.compute_weight(row, now, hard_word_ids={row["id"] + 999})

    assert empty == pytest.approx(base), (
        f"AC3c — empty hard_word_ids must produce the base weight; "
        f"base={base}, empty-kwarg result={empty}"
    )
    assert other_ids == pytest.approx(base), (
        f"AC3c — set excluding row id must produce the base weight; "
        f"base={base}, other-ids result={other_ids}"
    )


# === AC4 — /focus accepts focus:hard ========================================


def test_cmd_focus_accepts_focus_hard_alone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — /focus focus:hard → no ⚠️, focus_spec persisted with focus:hard
    _patch_bot(monkeypatch, conn)
    # Attach focus:hard to some word so the zero-match warning does not fire.
    vocab.add_word(conn, CHAT, "horse")
    _mark_focus_hard(conn, "horse")
    update = _make_command_update()

    asyncio.run(bot.cmd_focus(update, _make_context(["focus:hard"])))

    replies = _all_replies(update.message.reply_text)
    for r in replies:
        assert not r.startswith("⚠️"), (
            f"AC4 — /focus focus:hard must NOT trigger a ⚠️ reserved-label reply; "
            f"got {r!r}"
        )
    stored = vocab.get_focus_spec(conn, CHAT)
    assert stored is not None and "focus:hard" in stored, (
        f"AC4 — focus_spec must be persisted containing 'focus:hard'; got {stored!r}"
    )


def test_cmd_focus_accepts_focus_hard_with_other_label(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC4 — /focus --any focus:hard pos:noun → no ⚠️, both tokens persisted
    _patch_bot(monkeypatch, conn)
    # Ensure both labels match at least one word so zero-match warning silent.
    vocab.add_word(conn, CHAT, "horse")
    _mark_focus_hard(conn, "horse")
    _attach(conn, "horse", "pos:noun")
    update = _make_command_update()

    asyncio.run(bot.cmd_focus(update, _make_context(["--any", "focus:hard", "pos:noun"])))

    replies = _all_replies(update.message.reply_text)
    for r in replies:
        assert not r.startswith("⚠️"), (
            f"AC4 — /focus --any focus:hard pos:noun must NOT trigger ⚠️; "
            f"got {r!r}"
        )
    stored = vocab.get_focus_spec(conn, CHAT)
    assert stored is not None, "AC4 — focus_spec must persist"
    assert "focus:hard" in stored and "pos:noun" in stored, (
        f"AC4 — both tokens must appear in the persisted spec; got {stored!r}"
    )


# === AC5 — re-graduation strips focus:hard ==================================


def test_record_outcome_correct_threshold_detaches_focus_hard(
    conn: sqlite3.Connection,
) -> None:  # AC5a — correct call crossing REMEMBERED_THRESHOLD detaches focus:hard and attaches remembered
    word_id = _add_word(conn)
    _mark_focus_hard(conn, "apple")
    # Prime streak just below threshold so a single 1.0 push crosses it.
    conn.execute(
        "UPDATE words SET remembered_streak = 2.0 WHERE id = ?", (word_id,)
    )
    assert vocab.FOCUS_HARD_LABEL in vocab.labels_for_word(conn, word_id), (
        "precondition: word must carry `focus:hard`"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.REMEMBERED_LABEL in labels, (
        f"AC5a — threshold-crossing correct call must attach `remembered`; "
        f"got {labels!r}"
    )
    assert vocab.FOCUS_HARD_LABEL not in labels, (
        f"AC5a — threshold-crossing correct call must detach `focus:hard`; "
        f"got {labels!r}"
    )


def test_record_outcome_correct_below_threshold_keeps_focus_hard(
    conn: sqlite3.Connection,
) -> None:  # AC5b — correct call NOT crossing threshold leaves focus:hard intact
    word_id = _add_word(conn)
    _mark_focus_hard(conn, "apple")
    # Prime streak so it stays well below threshold after a +1.0 push.
    conn.execute(
        "UPDATE words SET remembered_streak = 0.5 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")

    labels = set(vocab.labels_for_word(conn, word_id))
    # Streak now 1.5 < 3.0 → no re-graduation.
    assert _streak(conn, word_id) == 1.5, (
        f"precondition: streak should now be 1.5; got {_streak(conn, word_id)}"
    )
    assert vocab.FOCUS_HARD_LABEL in labels, (
        f"AC5b — below-threshold correct call must NOT detach `focus:hard`; "
        f"got {labels!r}"
    )
    assert vocab.REMEMBERED_LABEL not in labels, (
        f"AC5b — below-threshold correct call must NOT attach `remembered`; "
        f"got {labels!r}"
    )


# === AC6 — hard_focus_word_ids and select_word boost ========================


def test_hard_focus_word_ids_empty(
    conn: sqlite3.Connection,
) -> None:  # AC6a — chat with no focus:hard words returns set()
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")

    result = vocab.hard_focus_word_ids(conn, CHAT)

    assert result == set(), (
        f"AC6a — chat with no focus:hard words must yield empty set; got {result!r}"
    )
    assert isinstance(result, set), (
        f"AC6a — return type must be `set`; got {type(result).__name__}"
    )


def test_hard_focus_word_ids_returns_ids(
    conn: sqlite3.Connection,
) -> None:  # AC6a / edge many — multiple focus:hard words → exact id set
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    vocab.add_word(conn, CHAT, "gamma")
    _mark_focus_hard(conn, "alpha")
    _mark_focus_hard(conn, "gamma")
    expected = {_word_id(conn, "alpha"), _word_id(conn, "gamma")}

    result = vocab.hard_focus_word_ids(conn, CHAT)

    assert result == expected, (
        f"AC6a — must return exactly the focus:hard word ids; "
        f"got {result!r}, expected {expected!r}"
    )


def test_hard_focus_word_ids_scopes_to_chat(
    conn: sqlite3.Connection,
) -> None:  # AC6a — focus:hard in another chat does not leak
    other = CHAT + 1
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, other, "alpha")  # same word text, different chat
    _mark_focus_hard(conn, "alpha", chat=other)

    assert vocab.hard_focus_word_ids(conn, CHAT) == set(), (
        "AC6a — focus:hard words in another chat must not leak into this chat"
    )
    assert vocab.hard_focus_word_ids(conn, other) == {
        _word_id(conn, "alpha", chat=other)
    }, "AC6a — the other chat's focus:hard word must still be returned for it"


def test_select_word_focus_hard_dominates(
    conn: sqlite3.Connection,
) -> None:  # AC6b — two-word pool, one with focus:hard → boosted word dominates picks
    vocab.add_word(conn, CHAT, "alpha")
    vocab.add_word(conn, CHAT, "beta")
    _mark_focus_hard(conn, "beta")

    rng = random.Random(0)
    picks = Counter(
        vocab.select_word(conn, CHAT, rng=rng)["text"] for _ in range(500)
    )

    # With both words at ~equal base weight ~8.0, the 2× boost gives beta ~16/24
    # of the probability mass ≈ 0.667. Allow generous slack for sampling noise
    # but require beta to be the clear majority well above the 50% no-boost mark.
    total = sum(picks.values())
    beta_fraction = picks["beta"] / total
    assert beta_fraction > 0.6, (
        f"AC6b — focus:hard word `beta` must dominate the picks under the 2× "
        f"boost (≈0.67 expected); got beta={picks['beta']}, alpha={picks['alpha']}, "
        f"fraction={beta_fraction:.3f}"
    )


# === edges + error =========================================================


def test_record_outcome_wrong_repeat_idempotent_existing_focus_hard(
    conn: sqlite3.Connection,
) -> None:  # edge — wrong/repeat on word already focus:hard → exactly one row, streak still 0
    word_id = _add_word(conn)
    _mark_focus_hard(conn, "apple")
    assert _word_label_count(conn, word_id, vocab.FOCUS_HARD_LABEL) == 1, (
        "precondition: exactly one focus:hard row attached"
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="repeat")

    assert _word_label_count(conn, word_id, vocab.FOCUS_HARD_LABEL) == 1, (
        f"edge — repeated wrong-repeat must not duplicate the focus:hard row; "
        f"got {_word_label_count(conn, word_id, vocab.FOCUS_HARD_LABEL)} rows"
    )
    assert _streak(conn, word_id) == 0.0, (
        f"edge — streak must still be 0 after idempotent attach; "
        f"got {_streak(conn, word_id)}"
    )


def test_record_outcome_mixed_sources_regraduate_detaches_focus_hard(
    conn: sqlite3.Connection,
) -> None:  # edge — focus:hard word re-graduates via 3 pushes after wrong-repeat: focus:hard detached on crossing
    word_id = _add_word(conn)
    _mark_focus_hard(conn, "apple")
    conn.execute(
        "UPDATE words SET remembered_streak = 0.0 WHERE id = ?", (word_id,)
    )

    for _ in range(2):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )
    # Streak now 2.0 < 3.0 — must still carry focus:hard.
    assert vocab.FOCUS_HARD_LABEL in vocab.labels_for_word(conn, word_id), (
        "edge — focus:hard must survive while streak is sub-threshold"
    )

    # Third push lifts streak to 3.0 → re-graduation.
    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")

    labels = set(vocab.labels_for_word(conn, word_id))
    assert vocab.REMEMBERED_LABEL in labels, (
        f"edge — threshold-crossing must attach `remembered`; got {labels!r}"
    )
    assert vocab.FOCUS_HARD_LABEL not in labels, (
        f"edge — threshold-crossing must detach `focus:hard` on mixed-sources "
        f"re-graduation; got {labels!r}"
    )


def test_compute_scores_empty_returns_empty_with_hard_word_ids() -> None:
    # edge — compute_scores([]) is [] regardless of hard_word_ids
    assert vocab.compute_scores([], hard_word_ids={1, 2, 3}) == [], (
        "edge — empty rows must yield [] even when hard_word_ids is non-empty"
    )
    assert vocab.compute_scores([], hard_word_ids=None) == [], (
        "edge — empty rows must yield [] with hard_word_ids=None"
    )


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
            conn, bogus_id, correct=False, weight=0.5, source="repeat"
        )
    assert excinfo.value.args[0] == bogus_id, (
        f"error — KeyError must carry the word_id; got args={excinfo.value.args!r}"
    )
