"""Daily-session integrity: answer classification, persistence, restart-safe
planning, and the shared in-progress gate between games and the cloze story.

Bot-layer mocking mirrors tests/test_games_cancel.py: Update/Context are
MagicMock + AsyncMock at the architectural seam, with `bot.conn` patched to
the temp-DB fixture from conftest.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import random
import sqlite3
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402
import cloze  # noqa: E402
import scheduler  # noqa: E402
import vocab  # noqa: E402


CHAT = 20400


# --- helpers -----------------------------------------------------------------


def _make_update(chat_id: int = CHAT, text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(data: str, chat_id: int = CHAT) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 1
    update.effective_user.is_bot = False
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _session(
    words: list[tuple[int, str]], *, push_id: int | None = None
) -> cloze.Session:
    story = " and ".join(f"a {w}" for _, w in words) + "."
    display, order, missing = cloze.blank_story(story, [w for _, w in words])
    assert not missing
    blanks = [
        cloze.Blank(word_id=words[i][0], word=words[i][1], is_intro=False)
        for i in order
    ]
    return cloze.Session(
        chat_id=CHAT, story=story, display=display, blanks=blanks, push_id=push_id
    )


def _seed_chat(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, active_start, "
        "active_end, tone, created_at) "
        "VALUES (?, 'UTC', 6, '00:00', '00:00', 'mixed', '2026-01-01')",
        (CHAT,),
    )


def _word_id(conn: sqlite3.Connection, text: str) -> int:
    wid = vocab.find_word_id(conn, CHAT, text)
    assert wid is not None
    return wid


def _reps(conn: sqlite3.Connection, word_id: int) -> int:
    return conn.execute(
        "SELECT reps FROM words WHERE id = ?", (word_id,)
    ).fetchone()["reps"]


def _last_reply(reply_text_mock: MagicMock) -> str:
    call = reply_text_mock.call_args_list[-1]
    return call.args[0] if call.args else call.kwargs["text"]


# --- cloze.classify_answer -----------------------------------------------------


def test_classify_bank_word_is_answer() -> None:
    s = _session([(1, "horse"), (2, "potent")])
    assert cloze.classify_answer("horse", s) == "answer"
    assert cloze.classify_answer("  Potent ", s) == "answer"


def test_classify_to_prefix_normalized() -> None:
    s = _session([(1, "to run out of")])
    assert cloze.classify_answer("run out of", s) == "answer"


def test_classify_skip_tokens() -> None:
    s = _session([(1, "horse")])
    assert cloze.classify_answer("skip", s) == "skip"
    assert cloze.classify_answer(" SKIP ", s) == "skip"
    assert cloze.classify_answer("?", s) == "skip"


def test_classify_stray_text_is_other() -> None:
    s = _session([(1, "horse")])
    assert cloze.classify_answer("hi, what does horse mean??", s) == "other"
    assert cloze.classify_answer("horze", s) == "other"  # typo ≠ silent Again


def test_not_answer_hint_names_current_blank() -> None:
    s = _session([(1, "horse"), (2, "potent")])
    s.current_blank = 1
    hint = cloze.format_not_answer_hint(s)
    assert "(2)" in hint and "skip" in hint


def test_blank_prompt_mentions_skip() -> None:
    s = _session([(1, "horse")])
    assert "skip" in cloze.format_blank_prompt(s)


# --- cloze session JSON roundtrip ---------------------------------------------


def test_session_json_roundtrip_preserves_progress() -> None:
    s = _session([(7, "horse"), (9, "potent")], push_id=42)
    cloze.apply_answer(s, False)  # miss the first blank
    restored = cloze.session_from_json(cloze.session_to_json(s))
    assert restored == s
    assert restored.current_blank == 1
    assert restored.wrong == [s.blanks[0].word]
    assert restored.push_id == 42


# --- scheduler.plan_session_time ------------------------------------------------


def test_plan_session_time_inside_window_after_now() -> None:
    zone = ZoneInfo("Europe/Warsaw")
    now = datetime.datetime(2026, 7, 7, 8, 0, tzinfo=zone)
    for seed in range(20):
        t = scheduler.plan_session_time(
            now.date(), "Europe/Warsaw", "11:00", "22:00",
            now=now, rng=random.Random(seed),
        )
        assert t is not None
        assert t.hour >= 11 and (t.hour, t.minute) < (22, 0)
        assert t > now


def test_plan_session_time_mid_window_samples_remaining() -> None:
    zone = ZoneInfo("Europe/Warsaw")
    now = datetime.datetime(2026, 7, 7, 20, 30, tzinfo=zone)  # restart at 20:30
    for seed in range(20):
        t = scheduler.plan_session_time(
            now.date(), "Europe/Warsaw", "11:00", "22:00",
            now=now, rng=random.Random(seed),
        )
        assert t is not None, "a passed slot must resample, not drop the day"
        assert t > now and (t.hour, t.minute) < (22, 0)


def test_plan_session_time_window_over_returns_none() -> None:
    zone = ZoneInfo("Europe/Warsaw")
    now = datetime.datetime(2026, 7, 7, 22, 30, tzinfo=zone)
    assert (
        scheduler.plan_session_time(
            now.date(), "Europe/Warsaw", "11:00", "22:00", now=now
        )
        is None
    )


def test_plan_session_time_midnight_end_rolls_over() -> None:
    zone = ZoneInfo("UTC")
    now = datetime.datetime(2026, 7, 7, 23, 0, tzinfo=zone)
    t = scheduler.plan_session_time(
        now.date(), "UTC", "09:00", "00:00", now=now, rng=random.Random(1)
    )
    assert t is not None and t > now


def test_plan_session_time_deterministic_with_seed() -> None:
    zone = ZoneInfo("UTC")
    now = datetime.datetime(2026, 7, 7, 1, 0, tzinfo=zone)
    a = scheduler.plan_session_time(
        now.date(), "UTC", "09:00", "21:00", now=now, rng=random.Random(5)
    )
    b = scheduler.plan_session_time(
        now.date(), "UTC", "09:00", "21:00", now=now, rng=random.Random(5)
    )
    assert a == b


# --- scheduler.sent_today --------------------------------------------------------


def test_sent_today_empty_log(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    assert not scheduler.sent_today(conn, CHAT, "UTC")


def test_sent_today_true_for_todays_row(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=[1])
    assert scheduler.sent_today(conn, CHAT, "UTC")


def test_sent_today_respects_local_date(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    # Sent 2026-07-06 23:30 UTC == 2026-07-07 01:30 in +02:00 — that's "today"
    # locally even though the UTC date is yesterday.
    conn.execute(
        "INSERT INTO push_log(chat_id, sent_at, word_ids_json) "
        "VALUES (?, '2026-07-06 23:30:00', '[1]')",
        (CHAT,),
    )
    zone = ZoneInfo("Europe/Warsaw")
    now = datetime.datetime(2026, 7, 7, 9, 0, tzinfo=zone)
    assert scheduler.sent_today(conn, CHAT, "Europe/Warsaw", now=now)
    assert not scheduler.sent_today(
        conn, CHAT, "Europe/Warsaw",
        now=datetime.datetime(2026, 7, 8, 9, 0, tzinfo=zone),
    )


# --- scheduler session persistence ----------------------------------------------


def test_unfinished_session_roundtrip(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=[1])
    s = _session([(1, "horse")], push_id=push_id)
    scheduler.save_session_json(conn, push_id, cloze.session_to_json(s))
    payload = scheduler.load_unfinished_session_json(conn, CHAT, "UTC")
    assert payload is not None
    assert cloze.session_from_json(payload) == s


def test_unfinished_session_ignores_rated(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=[1])
    scheduler.save_session_json(conn, push_id, "{}")
    scheduler.mark_rated(conn, push_id)
    assert scheduler.load_unfinished_session_json(conn, CHAT, "UTC") is None


def test_unfinished_session_ignores_yesterday(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    conn.execute(
        "INSERT INTO push_log(chat_id, sent_at, word_ids_json, session_json) "
        "VALUES (?, '2026-07-06 12:00:00', '[1]', '{}')",
        (CHAT,),
    )
    now = datetime.datetime(2026, 7, 7, 9, 0, tzinfo=ZoneInfo("UTC"))
    assert (
        scheduler.load_unfinished_session_json(conn, CHAT, "UTC", now=now) is None
    )


def test_delete_push_removes_row(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    push_id = scheduler.log_push(conn, CHAT, tg_message_id=None, word_ids=[1])
    scheduler.delete_push(conn, push_id)
    assert scheduler.load_push(conn, push_id) is None
    assert not scheduler.sent_today(conn, CHAT, "UTC")


def test_push_log_has_session_json_column(conn: sqlite3.Connection) -> None:
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(push_log)").fetchall()
    }
    assert "session_json" in cols


# --- PushRunner replanning --------------------------------------------------------


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}

    def add_job(self, func, trigger, args=None, id=None, replace_existing=False):
        self.jobs[id] = (func, trigger, args)

    def get_jobs(self):
        job = MagicMock()
        out = []
        for jid in self.jobs:
            j = MagicMock()
            j.id = jid
            out.append(j)
        return out

    def remove_job(self, jid):
        self.jobs.pop(jid, None)

    def start(self):
        pass


def _runner_settings():
    import config_flow

    return config_flow.Settings(
        tz="UTC", words_per_day=6, active_start="00:00",
        active_end="00:00", tone="mixed", target_lang="ru",
    )


def test_plan_today_skips_when_already_sent(conn: sqlite3.Connection) -> None:
    _seed_chat(conn)
    scheduler.log_push(conn, CHAT, tg_message_id=1, word_ids=[1])

    async def noop(chat_id):
        return None

    r = scheduler.PushRunner(conn, dispatch=noop, scheduler=_FakeScheduler())
    assert r._plan_today(CHAT, _runner_settings()) == []
    assert not any(j.startswith("push:") for j in r.scheduler.jobs)


def test_plan_today_schedules_one_job_when_not_sent(
    conn: sqlite3.Connection,
) -> None:
    _seed_chat(conn)

    async def noop(chat_id):
        return None

    r = scheduler.PushRunner(conn, dispatch=noop, scheduler=_FakeScheduler())
    times = r._plan_today(CHAT, _runner_settings())
    assert len(times) == 1
    assert f"push:{CHAT}:0" in r.scheduler.jobs


# --- bot: in-progress gate covers the story ---------------------------------------


def test_cmd_games_blocked_during_story(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.cloze_sessions[CHAT] = _session([(1, "horse")])
    update = _make_update()
    asyncio.run(bot.cmd_games(update, _make_context([])))
    assert _last_reply(update.message.reply_text) == bot.STORY_IN_PROGRESS


def test_games_menu_blocked_during_story(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    bot.cloze_sessions[CHAT] = _session([(1, "horse")])
    for data in ("gm:irr", "gm:repeat", "gm:focus", "gm:wt"):
        update = _make_callback_update(data)
        asyncio.run(bot.on_games_menu(update, _make_context()))
        assert (
            _last_reply(update.callback_query.message.reply_text)
            == bot.STORY_IN_PROGRESS
        ), f"{data} must be blocked during a story session"
    assert CHAT not in bot.irregulars
    assert CHAT not in bot.repeat_games


# --- bot: stray text must not be graded --------------------------------------------


def _seeded_session(conn: sqlite3.Connection, words: list[str]) -> cloze.Session:
    _seed_chat(conn)
    for w in words:
        vocab.add_word(conn, CHAT, w)
    push_id = scheduler.log_push(
        conn, CHAT, tg_message_id=1,
        word_ids=[_word_id(conn, w) for w in words],
    )
    s = _session([(_word_id(conn, w), w) for w in words], push_id=push_id)
    bot.cloze_sessions[CHAT] = s
    return s


def test_stray_text_not_graded(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    s = _seeded_session(conn, ["horse", "potent"])
    wid = s.current().word_id
    update = _make_update(text="hi, what does that mean?")
    asyncio.run(bot.handle_message(update, _make_context()))
    assert _reps(conn, wid) == 0, "stray chat text must not rate the blank"
    assert s.current_blank == 0, "session must not advance"
    assert "skip" in _last_reply(update.message.reply_text)


def test_skip_grades_as_miss(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    s = _seeded_session(conn, ["horse", "potent"])
    wid = s.current().word_id
    word = s.current().word
    update = _make_update(text="skip")
    asyncio.run(bot.handle_message(update, _make_context()))
    assert _reps(conn, wid) == 1
    assert s.current_blank == 1
    assert s.wrong == [word]


def test_wrong_bank_word_grades_as_miss(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    s = _seeded_session(conn, ["horse", "potent"])
    other = s.blanks[1].word
    update = _make_update(text=other)
    asyncio.run(bot.handle_message(update, _make_context()))
    assert s.current_blank == 1
    assert s.score == 0


def test_correct_answer_persists_progress(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    s = _seeded_session(conn, ["horse", "potent"])
    update = _make_update(text=s.current().word)
    asyncio.run(bot.handle_message(update, _make_context()))
    assert s.score == 1
    payload = scheduler.load_unfinished_session_json(conn, CHAT, "UTC")
    assert payload is not None
    assert cloze.session_from_json(payload).current_blank == 1


def test_cancel_closes_push_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    s = _seeded_session(conn, ["horse"])
    update = _make_update()
    asyncio.run(bot.cmd_games(update, _make_context(["cancel"])))
    assert CHAT not in bot.cloze_sessions
    row = scheduler.load_push(conn, s.push_id)
    assert row["rated"] == 1, "cancel must close the row so restart won't rehydrate"


# --- bot: dispatch deferral ---------------------------------------------------------


def test_dispatch_defers_while_typed_game_active(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    monkeypatch.setattr(bot, "app", MagicMock())
    bot.irregulars[CHAT] = MagicMock()
    asyncio.run(bot.dispatch_push(CHAT))
    assert CHAT in bot.deferred_sessions
    assert scheduler.load_unfinished_session_json(conn, CHAT, "UTC") is None


def test_deferred_session_fires_on_game_end(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot, "conn", conn)
    fired = AsyncMock()
    monkeypatch.setattr(bot, "dispatch_push", fired)
    bot.deferred_sessions.add(CHAT)
    asyncio.run(bot._fire_deferred_session(CHAT))
    fired.assert_awaited_once_with(CHAT)
    assert CHAT not in bot.deferred_sessions


def test_deferred_session_waits_while_game_still_active(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired = AsyncMock()
    monkeypatch.setattr(bot, "dispatch_push", fired)
    bot.deferred_sessions.add(CHAT)
    bot.repeat_games[CHAT] = MagicMock()
    asyncio.run(bot._fire_deferred_session(CHAT))
    fired.assert_not_awaited()
    assert CHAT in bot.deferred_sessions
