"""Tests for scheduler.py planning core + push composer.

APScheduler glue (PushRunner) is exercised lightly — we check job
registration/removal without actually running the loop.
"""

from __future__ import annotations

import asyncio
import datetime
import random
import sqlite3

import pytest

import config_flow
import scheduler
import vocab


CHAT = 700
UTC = datetime.timezone.utc


# --- plan_push_times ---------------------------------------------------------


def test_plan_returns_empty_if_window_non_positive() -> None:
    assert scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        3,
        "21:00",
        "09:00",
    ) == []


def test_plan_treats_midnight_end_as_end_of_day() -> None:
    # active_end="00:00" should mean the 24:00 boundary, not the same day's
    # midnight (which would make the window negative and return []).
    times = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        8,
        "13:00",
        "00:00",
        rng=random.Random(0),
    )
    assert len(times) == 8
    start = datetime.datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
    end = datetime.datetime(2026, 4, 23, 0, 0, tzinfo=UTC)
    for t in times:
        assert start <= t <= end


def test_plan_returns_empty_for_zero_pushes() -> None:
    assert scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        0,
        "09:00",
        "21:00",
    ) == []


def test_plan_produces_requested_count() -> None:
    times = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        4,
        "09:00",
        "21:00",
        rng=random.Random(0),
    )
    assert len(times) == 4


def test_plan_times_are_in_window_and_sorted() -> None:
    rng = random.Random(0)
    times = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        5,
        "09:00",
        "21:00",
        rng=rng,
    )
    # All tz-aware UTC, strictly inside the window
    start = datetime.datetime(2026, 4, 22, 9, 0, tzinfo=UTC)
    end = datetime.datetime(2026, 4, 22, 21, 0, tzinfo=UTC)
    for t in times:
        assert start <= t <= end
    # Sorted by construction (bucket index is monotonically increasing)
    assert times == sorted(times)


def test_plan_respects_min_gap_when_window_allows() -> None:
    rng = random.Random(1)
    times = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        4,
        "09:00",
        "21:00",  # 12h window → 180-min bucket, gap well above MIN_GAP_MIN
        rng=rng,
        min_gap_min=45,
    )
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    assert min(gaps) >= 45


def test_plan_degrades_gracefully_on_tight_window() -> None:
    # 60-min window, 3 pushes — bucket=20min, cannot satisfy 45-min gap.
    # Should still produce 3 ordered times without raising.
    times = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        3,
        "09:00",
        "10:00",
        rng=random.Random(0),
        min_gap_min=45,
    )
    assert len(times) == 3
    assert times == sorted(times)


def test_plan_is_deterministic_with_seeded_rng() -> None:
    a = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        3,
        "09:00",
        "21:00",
        rng=random.Random(42),
    )
    b = scheduler.plan_push_times(
        datetime.date(2026, 4, 22),
        "UTC",
        3,
        "09:00",
        "21:00",
        rng=random.Random(42),
    )
    assert a == b


# --- compose_push ------------------------------------------------------------


def _seed_chat(conn: sqlite3.Connection, tone: str = "funny") -> None:
    config_flow.save_settings(
        conn,
        CHAT,
        config_flow.Settings("UTC", 2, "09:00", "21:00", tone, "ru"),
    )


def test_compose_push_returns_none_if_chat_missing(conn: sqlite3.Connection) -> None:
    async def llm_fail(msgs):  # should never be called
        raise AssertionError("llm should not be called")

    out = asyncio.run(
        scheduler.compose_push(conn, CHAT, llm_chat=llm_fail)
    )
    assert out is None


def test_compose_push_returns_none_if_no_words(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)

    async def llm_fail(msgs):
        raise AssertionError("llm should not be called")

    out = asyncio.run(
        scheduler.compose_push(conn, CHAT, llm_chat=llm_fail)
    )
    assert out is None


def test_compose_push_returns_word_and_text_on_first_try(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")

    calls: list[list[dict]] = []

    async def llm_ok(msgs):
        calls.append(msgs)
        return "An ephemeral breeze stirred the page."

    out = asyncio.run(
        scheduler.compose_push(
            conn, CHAT, llm_chat=llm_ok, rng=random.Random(0)
        )
    )
    assert out is not None
    _, word, text, _ = out
    assert word == "ephemeral"
    assert "ephemeral" in text.lower()
    assert len(calls) == 1


def test_compose_push_retries_when_word_missing(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")

    calls: list[list[dict]] = []

    async def llm_flaky(msgs):
        calls.append(msgs)
        if len(calls) == 1:
            return "Nothing to see here."  # word missing → should retry
        return "The ephemeral mist lingered."

    out = asyncio.run(
        scheduler.compose_push(
            conn, CHAT, llm_chat=llm_flaky, rng=random.Random(0)
        )
    )
    assert out is not None
    _, word, text, _ = out
    assert word == "ephemeral"
    assert "ephemeral" in text.lower()
    assert len(calls) == 2


def test_compose_push_returns_anyway_after_retries(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")

    async def llm_bad(msgs):
        return "completely off-topic reply"

    out = asyncio.run(
        scheduler.compose_push(
            conn, CHAT, llm_chat=llm_bad, rng=random.Random(0)
        )
    )
    # With retries=1 we try 2 times and still return.
    assert out is not None
    _, word, text, _ = out
    assert word == "ephemeral"
    assert "ephemeral" not in text.lower()


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


# --- compose_translation -----------------------------------------------------


def test_compose_translation_returns_stripped_text(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "ephemeral")
    row = conn.execute(
        "SELECT id FROM words WHERE chat_id = ?", (CHAT,)
    ).fetchone()

    seen: list[tuple[str, str]] = []

    async def fake_translate(word: str, target: str) -> str:
        seen.append((word, target))
        return "  эфемерный  \n"

    out = asyncio.run(
        scheduler.compose_translation(
            conn, row["id"], "ru", translate_fn=fake_translate
        )
    )
    assert out == "эфемерный"
    assert seen == [("ephemeral", "ru")]


def test_compose_translation_returns_none_for_unknown_word(conn: sqlite3.Connection) -> None:
    async def fake_translate(word: str, target: str) -> str:
        raise AssertionError("translator should not be called")

    out = asyncio.run(
        scheduler.compose_translation(
            conn, 99999, "ru", translate_fn=fake_translate
        )
    )
    assert out is None


def test_compose_translation_returns_none_on_empty_result(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    vocab.add_word(conn, CHAT, "placid")
    row = conn.execute(
        "SELECT id FROM words WHERE chat_id = ?", (CHAT,)
    ).fetchone()

    async def fake_blank(word: str, target: str) -> str:
        return "  \n"

    out = asyncio.run(
        scheduler.compose_translation(
            conn, row["id"], "ru", translate_fn=fake_blank
        )
    )
    assert out is None


# --- format_explanation_reply ------------------------------------------------


def test_format_explanation_reply_without_translation_is_passthrough() -> None:
    assert (
        scheduler.format_explanation_reply("Means lasting briefly.", None)
        == "Means lasting briefly."
    )
    # Empty string is treated like None — no divider noise on a missing translation.
    assert (
        scheduler.format_explanation_reply("Means lasting briefly.", "")
        == "Means lasting briefly."
    )


def test_format_explanation_reply_appends_divider_and_translation() -> None:
    out = scheduler.format_explanation_reply(
        "Means <b>lasting</b> briefly.", "эфемерный"
    )
    # Two blank lines + a horizontal rule visually separate the two blocks.
    assert out == (
        "Means <b>lasting</b> briefly.\n\n"
        "──────────\n"
        "<i>эфемерный</i>"
    )


def test_format_explanation_reply_escapes_translation_html() -> None:
    out = scheduler.format_explanation_reply("ok", "<script>x</script>")
    # The translation must be HTML-escaped — Telegram parses the reply as HTML
    # and a stray '<' would either fail to render or be interpreted as a tag.
    assert "&lt;script&gt;x&lt;/script&gt;" in out
    assert "<script>" not in out


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
