"""Tests for issue #125 — vocab.record_outcome seam + remembered_streak column.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue.
record_outcome is the single chokepoint every word-outcome event passes through.
On `correct=True`, `weight` is added to `words.remembered_streak`. On
`correct=False`, the streak is reset to 0 (regardless of `weight`). Unknown
word_ids raise `KeyError`. The `source` argument is contractual (for future
hooks) but produces no state beyond the column update this cycle.

Bot-side integration tests cover the two call sites — `on_rate` (push,
weight=1.0) and `on_game_answer` (game, weight=0.5) — using the same mocking
pattern as `test_play_game_button.py`.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import games as games_module  # noqa: E402
import scheduler as sched_module  # noqa: E402
import vocab  # noqa: E402


CHAT = 4242


# -- helpers -----------------------------------------------------------------


def _add_word(conn: sqlite3.Connection, text: str = "apple") -> int:
    """Insert a chat row + a word row; return the word_id."""
    vocab.ensure_chat(conn, CHAT)
    vocab.add_word(conn, CHAT, text)
    return conn.execute(
        "SELECT id FROM words WHERE chat_id = ? AND text = ?", (CHAT, text)
    ).fetchone()["id"]


def _streak(conn: sqlite3.Connection, word_id: int) -> float:
    row = conn.execute(
        "SELECT remembered_streak FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    assert row is not None, f"no row for word_id={word_id}"
    return row["remembered_streak"]


# -- AC1 — correct/push adds weight to a streak-0 row ------------------------


def test_record_outcome_correct_push_increments_streak_by_weight(
    conn: sqlite3.Connection,
) -> None:  # AC1 — streak 0.0 → 1.0 after correct push @ weight=1.0
    word_id = _add_word(conn)
    assert _streak(conn, word_id) == 0.0, "precondition: fresh word starts at 0.0"

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")

    assert _streak(conn, word_id) == 1.0, (
        f"AC1 — correct push of weight=1.0 must add 1.0; got {_streak(conn, word_id)}"
    )


# -- AC2 — game/correct accrues additively (also: float exactness) -----------


def test_record_outcome_correct_game_accrues_additively(
    conn: sqlite3.Connection,
) -> None:  # AC2 — two games @ weight=0.5 → 1.0 (also edge: floats accumulate exactly)
    word_id = _add_word(conn)

    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")
    assert _streak(conn, word_id) == 0.5, (
        f"after first 0.5 game outcome, streak must be 0.5; got {_streak(conn, word_id)}"
    )

    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")
    assert _streak(conn, word_id) == 1.0, (
        f"AC2 — two 0.5 outcomes must accrue to exactly 1.0; got {_streak(conn, word_id)}"
    )


# -- AC3 — mixed sources accumulate ------------------------------------------


def test_record_outcome_mixed_sources_accumulate(
    conn: sqlite3.Connection,
) -> None:  # AC3 — push(+1.0) then game(+0.5) then game(+0.5) → 2.0
    word_id = _add_word(conn)

    vocab.record_outcome(conn, word_id, correct=True, weight=1.0, source="push")
    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")
    vocab.record_outcome(conn, word_id, correct=True, weight=0.5, source="game")

    assert _streak(conn, word_id) == 2.0, (
        f"AC3 — 1.0 + 0.5 + 0.5 must equal 2.0; got {_streak(conn, word_id)}"
    )


# -- AC4 — wrong resets streak (from nonzero AND from zero) ------------------


def test_record_outcome_wrong_resets_streak_from_nonzero(
    conn: sqlite3.Connection,
) -> None:  # AC4 — pre-existing streak=5.0 → 0.0 after one wrong outcome
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET remembered_streak = 5.0 WHERE id = ?", (word_id,)
    )
    assert _streak(conn, word_id) == 5.0, "precondition: streak primed to 5.0"

    vocab.record_outcome(conn, word_id, correct=False, weight=1.0, source="push")

    assert _streak(conn, word_id) == 0.0, (
        f"AC4 — wrong outcome must reset streak to 0.0; got {_streak(conn, word_id)}"
    )


def test_record_outcome_wrong_on_zero_streak_is_idempotent(
    conn: sqlite3.Connection,
) -> None:  # AC4 + edge: empty/no-op — wrong outcome on streak=0.0 leaves 0.0
    word_id = _add_word(conn)
    assert _streak(conn, word_id) == 0.0, "precondition: streak starts at 0.0"

    vocab.record_outcome(conn, word_id, correct=False, weight=1.0, source="push")

    assert _streak(conn, word_id) == 0.0, (
        f"AC4 — wrong outcome on already-0 streak must remain 0.0; got {_streak(conn, word_id)}"
    )


# -- AC5 — wrong reset ignores weight ----------------------------------------


def test_record_outcome_wrong_ignores_weight(
    conn: sqlite3.Connection,
) -> None:  # AC5 — wrong with weight=0.5 still resets to 0.0 (weight has no effect on reset)
    word_id = _add_word(conn)
    conn.execute(
        "UPDATE words SET remembered_streak = 3.5 WHERE id = ?", (word_id,)
    )

    vocab.record_outcome(conn, word_id, correct=False, weight=0.5, source="game")

    assert _streak(conn, word_id) == 0.0, (
        f"AC5 — reset is unconditional regardless of weight; got {_streak(conn, word_id)}"
    )


# -- AC6 / error: unknown word_id → KeyError ---------------------------------


def test_record_outcome_unknown_word_id_raises_keyerror(
    conn: sqlite3.Connection,
) -> None:  # AC6 + error: unknown word_id → KeyError(word_id)
    vocab.ensure_chat(conn, CHAT)
    bogus_id = 999_999
    pre = conn.execute(
        "SELECT 1 FROM words WHERE id = ?", (bogus_id,)
    ).fetchone()
    assert pre is None, "precondition: bogus word_id must NOT exist"

    with pytest.raises(KeyError) as excinfo:
        vocab.record_outcome(
            conn, bogus_id, correct=True, weight=1.0, source="push"
        )
    # Spec: KeyError(word_id). KeyError stringifies its arg with repr, so check
    # excinfo.value.args[0] for the actual int.
    assert excinfo.value.args[0] == bogus_id, (
        f"KeyError must carry the word_id; got args={excinfo.value.args!r}"
    )

    # The same must hold on the wrong-outcome path (rowcount==0 before reset).
    with pytest.raises(KeyError):
        vocab.record_outcome(
            conn, bogus_id, correct=False, weight=1.0, source="push"
        )


# -- AC9 — many sequential calls sum exactly ---------------------------------


def test_record_outcome_many_sequential_increments_sum_exactly(
    conn: sqlite3.Connection,
) -> None:  # AC9 — 10 sequential correct/1.0 calls → 10.0 (no lost updates)
    word_id = _add_word(conn)
    for _ in range(10):
        vocab.record_outcome(
            conn, word_id, correct=True, weight=1.0, source="push"
        )
    assert _streak(conn, word_id) == 10.0, (
        f"AC9 — 10 sequential +1.0 outcomes must sum to 10.0; got {_streak(conn, word_id)}"
    )


# -- AC10 — bot.on_rate fans into record_outcome -----------------------------


def _make_rate_update(verdict: str, push_id: int, word_id: int) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = f"rate:{verdict}:{push_id}:{word_id}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.effective_chat.id = CHAT
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    return update


def _seed_push(conn: sqlite3.Connection, word_id: int) -> int:
    return sched_module.log_push(
        conn, CHAT, tg_message_id=None, word_ids=[word_id]
    )


def _patch_bot_for_rate(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
) -> dict[str, MagicMock]:
    """Stub the side-effect seams in on_rate so the test isolates the
    record_outcome call. Returns the spies for assertion."""
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()

    rate_word_spy = MagicMock(wraps=vocab.rate_word)
    record_outcome_spy = MagicMock(wraps=vocab.record_outcome)
    explanation_spy = AsyncMock(return_value=None)  # short-circuit Again branch
    mark_rated_spy = MagicMock()

    monkeypatch.setattr(bot.vocab, "rate_word", rate_word_spy)
    monkeypatch.setattr(bot.vocab, "record_outcome", record_outcome_spy)
    monkeypatch.setattr(bot.sched_module, "compose_explanation", explanation_spy)
    monkeypatch.setattr(bot.sched_module, "mark_rated", mark_rated_spy)

    return {
        "rate_word": rate_word_spy,
        "record_outcome": record_outcome_spy,
        "mark_rated": mark_rated_spy,
    }


def test_on_rate_good_calls_record_outcome_correct_push(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC10 — verdict=good → record_outcome(True, 1.0, "push") + rate_word
    spies = _patch_bot_for_rate(monkeypatch, conn)
    word_id = _add_word(conn)
    push_id = _seed_push(conn, word_id)
    update = _make_rate_update("good", push_id, word_id)

    asyncio.run(bot.on_rate(update, MagicMock()))

    assert spies["record_outcome"].call_count == 1, (
        f"AC10 — record_outcome must be called exactly once on verdict=good; "
        f"got {spies['record_outcome'].call_count} calls"
    )
    call = spies["record_outcome"].call_args
    # Args may come positionally or as kwargs; normalise.
    bound = _bind_record_outcome(call)
    assert bound["word_id"] == word_id, (
        f"AC10 — record_outcome must receive the rated word_id; got {bound!r}"
    )
    assert bound["correct"] is True, (
        f"AC10 — verdict=good must produce correct=True; got {bound!r}"
    )
    assert bound["weight"] == 1.0, (
        f"AC10 — push outcomes use weight=1.0; got {bound!r}"
    )
    assert bound["source"] == "push", (
        f"AC10 — source must be 'push'; got {bound!r}"
    )
    assert spies["rate_word"].called, (
        "AC10 — vocab.rate_word must still be called (FSRS path unchanged)"
    )


def test_on_rate_again_calls_record_outcome_wrong_push(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC10 — verdict=again → record_outcome(False, 1.0, "push") + rate_word
    spies = _patch_bot_for_rate(monkeypatch, conn)
    word_id = _add_word(conn)
    push_id = _seed_push(conn, word_id)
    update = _make_rate_update("again", push_id, word_id)

    asyncio.run(bot.on_rate(update, MagicMock()))

    assert spies["record_outcome"].call_count == 1, (
        f"AC10 — record_outcome must be called exactly once on verdict=again; "
        f"got {spies['record_outcome'].call_count} calls"
    )
    bound = _bind_record_outcome(spies["record_outcome"].call_args)
    assert bound["word_id"] == word_id
    assert bound["correct"] is False, (
        f"AC10 — verdict=again must produce correct=False; got {bound!r}"
    )
    assert bound["weight"] == 1.0, (
        f"AC10 — push outcomes use weight=1.0; got {bound!r}"
    )
    assert bound["source"] == "push"
    assert spies["rate_word"].called, (
        "AC10 — vocab.rate_word must still be called on verdict=again"
    )


# -- AC11 — bot.on_game_answer fans into record_outcome ----------------------


def _bind_record_outcome(call) -> dict:
    """Coerce a MagicMock call_args into {conn, word_id, correct, weight, source}."""
    args = call.args
    kwargs = dict(call.kwargs)
    # Signature: record_outcome(conn, word_id, correct, weight, source)
    names = ("conn", "word_id", "correct", "weight", "source")
    bound: dict = {}
    for i, name in enumerate(names):
        if i < len(args):
            bound[name] = args[i]
        elif name in kwargs:
            bound[name] = kwargs[name]
    return bound


def _make_game_round(word_id: int, correct_index: int = 0) -> games_module.Round:
    return games_module.Round(
        word_id=word_id,
        prompt="cat",
        correct_answer="кошка",
        options=["кошка", "собака", "птица", "рыба"],
        correct_index=correct_index,
    )


def _make_game_update(round_idx: int, chosen_idx: int) -> MagicMock:
    update = MagicMock()
    update.callback_query.data = f"g:{round_idx}:{chosen_idx}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.effective_chat.id = CHAT
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    return update


def _patch_bot_for_game(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
) -> MagicMock:
    monkeypatch.setattr(bot, "conn", conn)
    bot.games.clear()
    record_outcome_spy = MagicMock(wraps=vocab.record_outcome)
    monkeypatch.setattr(bot.vocab, "record_outcome", record_outcome_spy)
    return record_outcome_spy


def test_on_game_answer_correct_calls_record_outcome_with_game(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC11 — correct round → record_outcome(rd.word_id, True, 0.5, "game")
    spy = _patch_bot_for_game(monkeypatch, conn)
    word_id_1 = _add_word(conn, "cat1")
    word_id_2 = _add_word(conn, "cat2")
    rd0 = _make_game_round(word_id_1, correct_index=2)
    rd1 = _make_game_round(word_id_2, correct_index=0)
    bot.games[CHAT] = games_module.Game(chat_id=CHAT, rounds=[rd0, rd1])

    # context.bot.send_message is awaited inside safe_send when game.done; with
    # 2 rounds and 1 answer we don't reach the done branch, but _send_round
    # still uses context.bot. Mock it.
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    update = _make_game_update(round_idx=0, chosen_idx=2)  # correct

    asyncio.run(bot.on_game_answer(update, context))

    assert spy.call_count == 1, (
        f"AC11 — record_outcome must be called exactly once per answered round; "
        f"got {spy.call_count} calls"
    )
    bound = _bind_record_outcome(spy.call_args)
    assert bound["word_id"] == word_id_1, (
        f"AC11 — must record the round's word_id ({word_id_1}); got {bound!r}"
    )
    assert bound["correct"] is True, (
        f"AC11 — correct tap must produce correct=True; got {bound!r}"
    )
    assert bound["weight"] == 0.5, (
        f"AC11 — game outcomes use weight=0.5; got {bound!r}"
    )
    assert bound["source"] == "game", (
        f"AC11 — source must be 'game'; got {bound!r}"
    )


def test_on_game_answer_wrong_calls_record_outcome_with_game(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC11 — wrong round still produces a record_outcome(False, 0.5, "game") call
    spy = _patch_bot_for_game(monkeypatch, conn)
    word_id_1 = _add_word(conn, "cat1")
    word_id_2 = _add_word(conn, "cat2")
    rd0 = _make_game_round(word_id_1, correct_index=2)
    rd1 = _make_game_round(word_id_2, correct_index=0)
    bot.games[CHAT] = games_module.Game(chat_id=CHAT, rounds=[rd0, rd1])

    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    update = _make_game_update(round_idx=0, chosen_idx=0)  # wrong (correct_index=2)

    asyncio.run(bot.on_game_answer(update, context))

    assert spy.call_count == 1, (
        f"AC11 — record_outcome must be called exactly once even on a wrong tap; "
        f"got {spy.call_count} calls"
    )
    bound = _bind_record_outcome(spy.call_args)
    assert bound["word_id"] == word_id_1
    assert bound["correct"] is False, (
        f"AC11 — wrong tap must produce correct=False; got {bound!r}"
    )
    assert bound["weight"] == 0.5
    assert bound["source"] == "game"


def test_on_game_answer_preserves_game_score(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:  # AC11 — in-memory game.score still increments on correct answers
    _patch_bot_for_game(monkeypatch, conn)
    word_id_1 = _add_word(conn, "cat1")
    word_id_2 = _add_word(conn, "cat2")
    rd0 = _make_game_round(word_id_1, correct_index=2)
    rd1 = _make_game_round(word_id_2, correct_index=0)
    game = games_module.Game(chat_id=CHAT, rounds=[rd0, rd1])
    bot.games[CHAT] = game
    assert game.score == 0, "precondition: fresh game starts at score=0"

    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    update = _make_game_update(round_idx=0, chosen_idx=2)  # correct

    asyncio.run(bot.on_game_answer(update, context))

    # The game gets popped from bot.games when done, but our 2-round game has
    # one round left after this answer, so it remains. We assert against the
    # local `game` reference either way.
    assert game.score == 1, (
        f"AC11 — game.score must still increment on correct answers (existing "
        f"behaviour preserved); got score={game.score}"
    )
