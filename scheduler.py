"""Push scheduling primitives and the APScheduler wrapper.

Split into a pure planning core (tested directly) and a thin `PushRunner`
wrapper that owns the AsyncIOScheduler. Production dispatch (sending on
Telegram, attaching rating buttons) is injected by `bot.py` so this module
stays free of telegram imports.

`plan_push_times` picks randomized times inside the active window using
equal-sized buckets with a half-gap buffer at bucket edges — so consecutive
pushes can't cluster against the boundary.

`compose_push` runs one cycle: select a word, prompt the LLM, retry once if
the word didn't appear literally. Persistence of the sent message is the
caller's job so bot.py can include the Telegram message id.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import random
import sqlite3
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import config_flow
import prompts
import vocab


log = logging.getLogger(__name__)

# Minimum gap between consecutive pushes (minutes). Enforced implicitly by the
# bucket algorithm below.
MIN_GAP_MIN = 45


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def plan_push_times(
    date: datetime.date,
    tz: str,
    pushes_per_day: int,
    active_start: str,
    active_end: str,
    *,
    rng: random.Random | None = None,
    min_gap_min: int = MIN_GAP_MIN,
) -> list[datetime.datetime]:
    """Pick `pushes_per_day` tz-aware datetimes within the active window.

    Divides the window into equal buckets and samples one point per bucket
    with a half-gap buffer at each bucket edge. Returns [] if the window is
    zero-length or negative.
    """
    rng = rng or random
    zone = ZoneInfo(tz)
    sh, sm = _parse_hm(active_start)
    eh, em = _parse_hm(active_end)
    start = datetime.datetime.combine(date, datetime.time(sh, sm), tzinfo=zone)
    end = datetime.datetime.combine(date, datetime.time(eh, em), tzinfo=zone)
    # "00:00" as active_end means end-of-day (the 24:00 boundary), not the
    # same day's midnight — roll it forward one day so the window is positive.
    if (eh, em) == (0, 0):
        end += datetime.timedelta(days=1)
    window_min = int((end - start).total_seconds() // 60)
    if window_min <= 0 or pushes_per_day <= 0:
        return []

    bucket = window_min / pushes_per_day
    half_gap = min_gap_min / 2 if bucket > min_gap_min else 0.0

    times: list[datetime.datetime] = []
    for i in range(pushes_per_day):
        lo = int(i * bucket + half_gap)
        hi = int((i + 1) * bucket - half_gap) - 1
        if hi < lo:
            # Bucket too tight for the buffer; place at bucket center.
            off = int(i * bucket + bucket / 2)
        else:
            off = rng.randint(lo, hi)
        times.append(start + datetime.timedelta(minutes=off))
    return times


# Injected type aliases for clarity.
LlmChat = Callable[[list[dict]], Awaitable[str]]
TranslateFn = Callable[[str, str], Awaitable[str]]


async def compose_push(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    llm_chat: LlmChat,
    names: list[str] | None = None,
    mode: Literal["all", "any"] = "all",
    rng: random.Random | None = None,
    now: datetime.datetime | None = None,
    retries: int = 1,
) -> tuple[int, str, str] | None:
    """Select a word and prompt the LLM. Returns (word_id, word, text) or None.

    `names`, when non-empty, restricts the candidate pool via
    `vocab.select_word`. `mode="all"` (default) requires every label;
    `mode="any"` requires at least one. A filter that yields zero matches
    returns None — the caller logs and skips the push.

    Retries once if the word doesn't appear literally in the output. On final
    failure the text is returned anyway — rare with short vocab prompts and
    better than dropping the push.
    """
    chat_row = conn.execute(
        "SELECT tone FROM chats WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if chat_row is None:
        return None
    picked = vocab.select_word(
        conn, chat_id, names=names, mode=mode, rng=rng, now=now
    )
    if picked is None:
        return None

    tone = chat_row["tone"]
    word = picked["text"]
    text = ""
    for _ in range(retries + 1):
        text = await llm_chat(prompts.push_messages(word, tone, rng=rng))
        if word in text.lower():
            break
    return picked["id"], word, text


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


async def compose_translation(
    conn: sqlite3.Connection,
    word_id: int,
    target: str,
    *,
    translate_fn: TranslateFn,
) -> str | None:
    """Translate the rated word into `target` (ISO code) for the ❌-forgot reply.

    Mirrors `compose_explanation`'s contract: returns the stripped translation,
    or None when the word is gone or the result is empty. Callers wrap the
    sync `translator.translate` in `asyncio.to_thread` to satisfy `translate_fn`.
    """
    row = conn.execute(
        "SELECT text FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    if row is None:
        return None
    text = (await translate_fn(row["text"], target)).strip()
    return text or None


# Visual divider between the LLM explanation and the single-word translation.
# A box-drawing rule keeps the two sections distinct without leaning on emoji.
_EXPLAIN_DIVIDER = "──────────"


def format_explanation_reply(
    explanation_html: str, translation: str | None
) -> str:
    """Compose the ❌-forgot reply body — explanation, then optional translation.

    `explanation_html` is already HTML-safe (typically from `vocab.highlight_matches`).
    `translation` is plain text and gets HTML-escaped here. When translation is
    None or empty the reply is just the explanation, unchanged.
    """
    if not translation:
        return explanation_html
    return (
        f"{explanation_html}\n\n"
        f"{_EXPLAIN_DIVIDER}\n"
        f"<i>{html.escape(translation)}</i>"
    )


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
        times = plan_push_times(
            now.date(),
            settings.tz,
            settings.pushes_per_day,
            settings.active_start,
            settings.active_end,
        )
        scheduled: list[datetime.datetime] = []
        for i, t in enumerate(times):
            if t <= now:
                continue
            self.scheduler.add_job(
                self.dispatch,
                DateTrigger(run_date=t),
                args=[chat_id],
                id=f"push:{chat_id}:{i}",
                replace_existing=True,
            )
            scheduled.append(t)
        log.info(
            "Planned %d pushes for chat %s today: %s",
            len(scheduled),
            chat_id,
            [t.isoformat(timespec="minutes") for t in scheduled],
        )
        return scheduled

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
