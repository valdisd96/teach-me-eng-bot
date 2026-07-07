"""Push scheduling primitives and the APScheduler wrapper.

Split into a pure planning core (tested directly) and a thin `PushRunner`
wrapper that owns the AsyncIOScheduler. Production dispatch (sending on
Telegram, attaching rating buttons) is injected by `bot.py` so this module
stays free of telegram imports.

`plan_session_time` picks one randomized time inside the active window,
sampling only the remaining window when called mid-day (restart-safe).

`compose_session` builds the once-daily cloze-story session: select the
day's words, prompt the LLM for a story, retry once if any word didn't
appear literally, and blank the found words. Persistence of the sent
message is the caller's job so bot.py can include the Telegram message id.
"""

from __future__ import annotations

import datetime
import json
import logging
import random
import sqlite3
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import cloze
import config_flow
import prompts
import vocab


log = logging.getLogger(__name__)

def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def plan_session_time(
    date: datetime.date,
    tz: str,
    active_start: str,
    active_end: str,
    *,
    now: datetime.datetime | None = None,
    rng: random.Random | None = None,
) -> datetime.datetime | None:
    """One uniform-random minute inside the active window, strictly after `now`.

    A mid-window `now` (bot restarted after the original slot passed) samples
    the *remaining* window instead of dropping the day. Returns None when the
    window has already closed (or is empty).
    """
    zone = ZoneInfo(tz)
    sh, sm = _parse_hm(active_start)
    eh, em = _parse_hm(active_end)
    start = datetime.datetime.combine(date, datetime.time(sh, sm), tzinfo=zone)
    end = datetime.datetime.combine(date, datetime.time(eh, em), tzinfo=zone)
    # "00:00" as active_end means end-of-day (the 24:00 boundary), not the
    # same day's midnight — roll it forward one day so the window is positive.
    if (eh, em) == (0, 0):
        end += datetime.timedelta(days=1)
    now = now or datetime.datetime.now(zone)
    # Leave a one-minute floor so the job never lands in the past by the time
    # APScheduler registers it.
    lo = max(start, now + datetime.timedelta(minutes=1))
    window_min = int((end - lo).total_seconds() // 60)
    if window_min <= 0:
        return None
    off = (rng or random).randint(0, window_min - 1)
    return lo + datetime.timedelta(minutes=off)


_SENT_AT_FMT = "%Y-%m-%d %H:%M:%S"


def sent_today(
    conn: sqlite3.Connection,
    chat_id: int,
    tz: str,
    *,
    now: datetime.datetime | None = None,
) -> bool:
    """True if the chat's latest push_log row falls on today's *local* date.

    Guards replanning after a restart: without it every service restart
    re-rolls the daily session and can send a second story the same day.
    """
    row = conn.execute(
        "SELECT sent_at FROM push_log WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if row is None:
        return False
    zone = ZoneInfo(tz)
    sent = datetime.datetime.strptime(row["sent_at"], _SENT_AT_FMT).replace(
        tzinfo=datetime.timezone.utc
    )
    now = now or datetime.datetime.now(zone)
    return sent.astimezone(zone).date() == now.date()


# Injected type alias for clarity.
LlmChat = Callable[[list[dict]], Awaitable[str]]


async def compose_session(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    llm_chat: LlmChat,
    n_words: int,
    names: list[str] | None = None,
    mode: Literal["all", "any"] = "all",
    rng: random.Random | None = None,
    now: datetime.datetime | None = None,
    retries: int = 1,
) -> cloze.Session | None:
    """Build the daily cloze-story session. Returns a `cloze.Session` or None.

    Words come from `vocab.select_session_words`: up to
    `cloze.MAX_INTRO_WORDS` introduction-phase picks bypass the `names`
    /focus filter, the rest respect it. The LLM writes one story using every
    word literally; if any word is missing from the story the call is
    retried once, and words still missing after that are dropped from the
    session (logged) rather than blocking it. Returns None when no words are
    available, the chat is unconfigured, or no word made it into the story.
    """
    chat_row = conn.execute(
        "SELECT tone FROM chats WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if chat_row is None:
        return None
    picked = vocab.select_session_words(
        conn,
        chat_id,
        n_words,
        names=names,
        mode=mode,
        max_intro=cloze.MAX_INTRO_WORDS,
        rng=rng,
        now=now,
    )
    if not picked:
        return None

    texts = [r["text"] for r in picked]
    # Resolve "mixed" once so a retry keeps the same tone as the attempt it
    # is fixing, and keep the BEST attempt — a retry that omits more words
    # than the original must not win.
    tone = prompts.resolve_tone(chat_row["tone"], rng=rng)
    story, display, order, missing = "", "", [], list(texts)
    prev_missing: list[str] | None = None
    for _ in range(retries + 1):
        attempt_story = await llm_chat(
            prompts.story_messages(texts, tone, rng=rng, missing=prev_missing)
        )
        a_display, a_order, a_missing = cloze.blank_story(attempt_story, texts)
        if prev_missing is None or len(a_missing) < len(missing):
            story, display, order, missing = (
                attempt_story, a_display, a_order, a_missing
            )
        if not missing:
            break
        prev_missing = a_missing
    if missing:
        log.warning(
            "Session for chat %s: dropping words missing from story: %s",
            chat_id,
            missing,
        )
    if not order:
        return None
    blanks = [
        cloze.Blank(
            word_id=picked[i]["id"],
            word=picked[i]["text"],
            is_intro=picked[i]["reps"] < vocab.INTRO_GRADUATION_REPS,
            translation=picked[i]["translation"],
        )
        for i in order
    ]
    return cloze.Session(
        chat_id=chat_id, story=story, display=display, blanks=blanks
    )


async def compose_explanation(
    conn: sqlite3.Connection,
    word_id: int,
    *,
    llm_chat: LlmChat,
) -> str | None:
    """Fetch the word text and ask the LLM for a short meaning + example.

    Returns the stripped explanation, or None if the word no longer exists or
    the LLM returned nothing usable. The caller is responsible for actually
    delivering the text (e.g. via Telegram) — keeping the HTTP side out of
    this module makes it testable without mocks.
    """
    row = conn.execute(
        "SELECT text FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    if row is None:
        return None
    text = (await llm_chat(prompts.explain_messages(row["text"]))).strip()
    return text or None


def log_push(
    conn: sqlite3.Connection,
    chat_id: int,
    tg_message_id: int | None,
    word_ids: list[int],
) -> int:
    """Insert a push_log row; returns its id."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO push_log(chat_id, sent_at, tg_message_id, word_ids_json) "
        "VALUES (?, ?, ?, ?)",
        (chat_id, now, tg_message_id, json.dumps(word_ids)),
    )
    return cur.lastrowid


def mark_rated(conn: sqlite3.Connection, push_id: int) -> None:
    conn.execute("UPDATE push_log SET rated = 1 WHERE id = ?", (push_id,))


def delete_push(conn: sqlite3.Connection, push_id: int) -> None:
    """Drop a push_log row whose Telegram send failed.

    Leaving the phantom row would make `sent_today` suppress a replan, so a
    single failed send would silently cost the chat its whole day.
    """
    conn.execute("DELETE FROM push_log WHERE id = ?", (push_id,))


def save_session_json(
    conn: sqlite3.Connection, push_id: int, session_json: str
) -> None:
    """Persist the in-flight cloze session (called after send + every answer)."""
    conn.execute(
        "UPDATE push_log SET session_json = ? WHERE id = ?",
        (session_json, push_id),
    )


def load_unfinished_session_json(
    conn: sqlite3.Connection,
    chat_id: int,
    tz: str,
    *,
    now: datetime.datetime | None = None,
) -> str | None:
    """session_json of today's still-unrated session, or None.

    Yesterday's leftovers are ignored — the answers would rate against a
    stale word selection, and today's dispatch replaces the session anyway.
    """
    row = conn.execute(
        "SELECT sent_at, session_json FROM push_log "
        "WHERE chat_id = ? AND rated = 0 AND session_json IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if row is None:
        return None
    zone = ZoneInfo(tz)
    sent = datetime.datetime.strptime(row["sent_at"], _SENT_AT_FMT).replace(
        tzinfo=datetime.timezone.utc
    )
    now = now or datetime.datetime.now(zone)
    if sent.astimezone(zone).date() != now.date():
        return None
    return row["session_json"]


def load_push(
    conn: sqlite3.Connection, push_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM push_log WHERE id = ?", (push_id,)
    ).fetchone()


# --- APScheduler wrapper -----------------------------------------------------

Dispatch = Callable[[int], Awaitable[None]]


class PushRunner:
    """Owns the AsyncIOScheduler and keeps per-chat jobs in sync with settings.

    Two job kinds are registered per chat:
      * `plan:<chat_id>` — fires daily at 00:01 local, re-rolls today's push
        times (so they vary each day).
      * `push:<chat_id>:<i>` — one-shot jobs for each planned time today.

    `dispatch` is injected by bot.py and is an `async (chat_id) -> None` that
    runs the actual compose + send + log_push flow.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        dispatch: Dispatch,
        *,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self.conn = conn
        self.dispatch = dispatch
        self.scheduler = scheduler or AsyncIOScheduler()

    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)

    def schedule_chat(
        self, chat_id: int, settings: config_flow.Settings
    ) -> list[datetime.datetime]:
        """(Re)register all jobs for a chat; returns the push times scheduled today."""
        self._remove_chat_jobs(chat_id)
        zone = ZoneInfo(settings.tz)
        today_times = self._plan_today(chat_id, settings)
        self.scheduler.add_job(
            self._plan_today_job,
            CronTrigger(hour=0, minute=1, timezone=zone),
            args=[chat_id, settings],
            id=f"plan:{chat_id}",
            replace_existing=True,
        )
        return today_times

    def _plan_today_job(
        self, chat_id: int, settings: config_flow.Settings
    ) -> None:
        self._plan_today(chat_id, settings)

    def _plan_today(
        self, chat_id: int, settings: config_flow.Settings
    ) -> list[datetime.datetime]:
        self._remove_push_jobs(chat_id)
        zone = ZoneInfo(settings.tz)
        now = datetime.datetime.now(zone)
        # One daily cloze-story session; words_per_day sizes the story, not
        # the schedule. Restart-safe: skip if today's session already went
        # out, otherwise sample the remaining window (not the full day).
        if sent_today(self.conn, chat_id, settings.tz, now=now):
            log.info("Session already sent today for chat %s; not replanning", chat_id)
            return []
        t = plan_session_time(
            now.date(),
            settings.tz,
            settings.active_start,
            settings.active_end,
            now=now,
        )
        if t is None:
            log.info("Active window already over for chat %s today", chat_id)
            return []
        self.scheduler.add_job(
            self.dispatch,
            DateTrigger(run_date=t),
            args=[chat_id],
            id=f"push:{chat_id}:0",
            replace_existing=True,
        )
        log.info(
            "Planned session for chat %s today: %s",
            chat_id,
            t.isoformat(timespec="minutes"),
        )
        return [t]

    def _remove_chat_jobs(self, chat_id: int) -> None:
        for j in list(self.scheduler.get_jobs()):
            if j.id.startswith(f"plan:{chat_id}") or j.id.startswith(
                f"push:{chat_id}:"
            ):
                self.scheduler.remove_job(j.id)

    def _remove_push_jobs(self, chat_id: int) -> None:
        for j in list(self.scheduler.get_jobs()):
            if j.id.startswith(f"push:{chat_id}:"):
                self.scheduler.remove_job(j.id)

    def refresh_all(self) -> None:
        """Rebuild every chat's jobs — call on bot startup."""
        rows = self.conn.execute(
            "SELECT chat_id FROM chats"
        ).fetchall()
        for r in rows:
            settings = config_flow.load_settings(self.conn, r["chat_id"])
            if settings is None:
                continue
            self.schedule_chat(r["chat_id"], settings)
