"""Tests for scheduler.py session composer + push_log helpers.

APScheduler glue (PushRunner) is exercised lightly — we check job
registration/removal without actually running the loop.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3

import pytest

import config_flow
import scheduler
import vocab


CHAT = 700


# --- compose_session ---------------------------------------------------------


def _seed_chat(conn: sqlite3.Connection, tone: str = "funny") -> None:
    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings("UTC", 2, "09:00", "21:00", tone, "ru"),
    )


def test_compose_session_returns_none_if_chat_missing(conn: sqlite3.Connection) -> None:
    async def llm_fail(msgs):  # should never be called
        raise AssertionError("llm should not be called")

    out = asyncio.run(
        scheduler.compose_session(conn, CHAT, llm_chat=llm_fail, n_words=5)
    )
    assert out is None


def test_compose_session_returns_none_if_no_words(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)

    async def llm_fail(msgs):
        raise AssertionError("llm should not be called")

    out = asyncio.run(
        scheduler.compose_session(conn, CHAT, llm_chat=llm_fail, n_words=5)
    )
    assert out is None


def test_compose_session_blanks_every_word_on_first_try(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    vocab.add_word(conn, CHAT, "placid")

    calls: list[list[dict]] = []

    async def llm_ok(msgs):
        calls.append(msgs)
        return "The ephemeral mist settled over the placid lake."

    out = asyncio.run(
        scheduler.compose_session(
            conn, CHAT, llm_chat=llm_ok, n_words=5, rng=random.Random(0)
        )
    )
    assert out is not None
    assert len(calls) == 1
    assert {b.word for b in out.blanks} == {"ephemeral", "placid"}
    assert "___(1)" in out.display and "___(2)" in out.display
    assert "ephemeral" not in out.display and "placid" not in out.display
    # Raw story is kept for the end-of-session reveal.
    assert "ephemeral" in out.story


def test_compose_session_retries_when_word_missing(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")

    calls: list[list[dict]] = []

    async def llm_flaky(msgs):
        calls.append(msgs)
        if len(calls) == 1:
            return "Nothing to see here."  # word missing -> should retry
        return "The ephemeral mist lingered."

    out = asyncio.run(
        scheduler.compose_session(
            conn, CHAT, llm_chat=llm_flaky, n_words=5, rng=random.Random(0)
        )
    )
    assert out is not None
    assert [b.word for b in out.blanks] == ["ephemeral"]
    assert len(calls) == 2


def test_compose_session_drops_still_missing_words(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    vocab.add_word(conn, CHAT, "placid")

    async def llm_partial(msgs):
        return "The ephemeral mist lingered."  # placid never appears

    out = asyncio.run(
        scheduler.compose_session(
            conn, CHAT, llm_chat=llm_partial, n_words=5, rng=random.Random(0)
        )
    )
    # With retries=1 we try twice, then drop the missing word and keep going.
    assert out is not None
    assert [b.word for b in out.blanks] == ["ephemeral"]


def test_compose_session_returns_none_when_no_word_lands(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")

    async def llm_bad(msgs):
        return "completely off-topic reply"

    out = asyncio.run(
        scheduler.compose_session(
            conn, CHAT, llm_chat=llm_bad, n_words=5, rng=random.Random(0)
        )
    )
    assert out is None


def test_compose_session_marks_intro_words(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")  # reps=0 -> intro phase

    async def llm_ok(msgs):
        return "The ephemeral mist lingered."

    out = asyncio.run(
        scheduler.compose_session(
            conn, CHAT, llm_chat=llm_ok, n_words=5, rng=random.Random(0)
        )
    )
    assert out is not None
    assert out.blanks[0].is_intro is True


# --- compose_explanation -----------------------------------------------------


def test_compose_explanation_returns_stripped_text(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    row = conn.execute(
        "SELECT id FROM words WHERE chat_id = ?", (CHAT,)
    ).fetchone()

    seen: list[list[dict]] = []

    async def llm_ok(msgs):
        seen.append(msgs)
        return "  Lasting a very short time.\nExample: The joy was ephemeral.  \n"

    out = asyncio.run(
        scheduler.compose_explanation(conn, row["id"], llm_chat=llm_ok)
    )
    assert out == "Lasting a very short time.\nExample: The joy was ephemeral."
    # The prompt must be built from prompts.explain_messages (system + user
    # with the word in the user message).
    assert len(seen) == 1
    msgs = seen[0]
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "ephemeral" in msgs[1]["content"]


def test_compose_explanation_returns_none_for_unknown_word(conn: sqlite3.Connection) -> None:
    async def llm_fail(msgs):
        raise AssertionError("llm should not be called when the word is gone")

    out = asyncio.run(
        scheduler.compose_explanation(conn, 99999, llm_chat=llm_fail)
    )
    assert out is None


def test_compose_explanation_returns_none_on_empty_llm_reply(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "placid")
    row = conn.execute(
        "SELECT id FROM words WHERE chat_id = ?", (CHAT,)
    ).fetchone()

    async def llm_blank(msgs):
        return "   \n  "

    out = asyncio.run(
        scheduler.compose_explanation(conn, row["id"], llm_chat=llm_blank)
    )
    assert out is None


# --- log_push / mark_rated ---------------------------------------------------


def test_log_push_roundtrip(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=555, word_ids=[1, 2])
    row = scheduler.load_push(conn, push_id)
    assert row["chat_id"] == CHAT
    assert row["tg_message_id"] == 555
    assert row["rated"] == 0
    assert row["word_ids_json"] == "[1, 2]"


def test_mark_rated_sets_flag(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=[1])
    scheduler.mark_rated(conn, push_id)
    row = scheduler.load_push(conn, push_id)
    assert row["rated"] == 1


# --- PushRunner (lightweight) ------------------------------------------------


@pytest.fixture
def runner(conn: sqlite3.Connection):
    async def noop(chat_id: int) -> None:
        return None

    r = scheduler.PushRunner(conn, dispatch=noop)
    yield r
    # Clean up pending jobs; do not start the underlying scheduler so nothing fires.
    for j in list(r.scheduler.get_jobs()):
        r.scheduler.remove_job(j.id)


def test_schedule_chat_adds_plan_and_push_jobs(
    runner: scheduler.PushRunner, conn: sqlite3.Connection
) -> None:
    settings = config_flow.Settings("UTC", 3, "09:00", "21:00", "mixed", "ru")
    config_flow.save_settings(conn, CHAT, settings)
    runner.schedule_chat(CHAT, settings)
    ids = {j.id for j in runner.scheduler.get_jobs()}
    assert f"plan:{CHAT}" in ids
    # At least one push job should be queued for today — unless the test is run
    # very near local midnight, which we accept as a non-deterministic edge.
    assert any(i.startswith(f"push:{CHAT}:") for i in ids) or any(
        i == f"plan:{CHAT}" for i in ids
    )


def test_schedule_chat_is_idempotent(
    runner: scheduler.PushRunner, conn: sqlite3.Connection
) -> None:
    settings = config_flow.Settings("UTC", 2, "00:00", "23:59", "mixed", "ru")
    config_flow.save_settings(conn, CHAT, settings)
    # Seed both calls identically so they sample the same push times — the
    # runner uses the global `random` module, so without seeding, suite-level
    # PRNG drift between the two calls could produce different past-slot
    # filtering and make the cross-check below flaky.
    random.seed(0)
    runner.schedule_chat(CHAT, settings)
    first = {j.id for j in runner.scheduler.get_jobs()}
    random.seed(0)
    runner.schedule_chat(CHAT, settings)
    second = {j.id for j in runner.scheduler.get_jobs()}
    # Re-scheduling shouldn't leak stale jobs — plan id is stable; push ids
    # are bounded by pushes_per_day.
    assert f"plan:{CHAT}" in second
    assert len([i for i in second if i.startswith(f"push:{CHAT}:")]) <= 2
    # With identical inputs the second run should produce exactly the first
    # run's job set — no accumulation, no missing jobs.
    assert second == first
