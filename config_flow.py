"""Per-chat settings and the /start conversation state machine.

Kept separate from Telegram so the step logic can be unit-tested. `bot.py`
(PR6) owns the wiring — it creates a `ConfigSession` on `/start`, feeds it
each plain-text reply, and on `done` persists via `save_settings`.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from dataclasses import dataclass, asdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TONES = ("funny", "motivational", "scary", "bright", "mixed")
MIN_PUSHES = 1
MAX_PUSHES = 8


@dataclass
class Settings:
    tz: str
    pushes_per_day: int
    active_start: str  # "HH:MM"
    active_end: str
    tone: str


def validate_tz(s: str) -> str:
    name = s.strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown timezone {name!r}. Try e.g. Europe/Warsaw or UTC."
        ) from exc
    return name


def validate_pushes(s: str) -> int:
    try:
        n = int(s.strip())
    except ValueError as exc:
        raise ValueError(
            f"Send a whole number between {MIN_PUSHES} and {MAX_PUSHES}."
        ) from exc
    if not MIN_PUSHES <= n <= MAX_PUSHES:
        raise ValueError(f"Pick a number between {MIN_PUSHES} and {MAX_PUSHES}.")
    return n


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def validate_hhmm(s: str) -> str:
    m = _HHMM_RE.match(s.strip())
    if not m:
        raise ValueError("Use 24h HH:MM format, e.g. 09:00 or 21:30.")
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}"


def validate_tone(s: str) -> str:
    v = s.strip().lower()
    if v not in TONES:
        raise ValueError(f"Pick one of: {', '.join(TONES)}.")
    return v


# Step definitions: (field, prompt, validator).
_STEPS = [
    ("tz", "What timezone? (e.g. Europe/Warsaw, UTC)", validate_tz),
    (
        "pushes_per_day",
        f"How many pushes per day? ({MIN_PUSHES}–{MAX_PUSHES})",
        validate_pushes,
    ),
    (
        "active_start",
        "Active window start? (HH:MM, e.g. 09:00)",
        validate_hhmm,
    ),
    (
        "active_end",
        "Active window end? (HH:MM, e.g. 21:00)",
        validate_hhmm,
    ),
    (
        "tone",
        "Tone preference? (funny / motivational / scary / bright / mixed)",
        validate_tone,
    ),
]

INTRO = "Let's configure your vocab agent.\n\n"
DONE_MESSAGE = (
    "✅ All set. Add words with `/add <word>` and pushes will start "
    "showing up in your active window."
)


class ConfigSession:
    """Tracks a single chat's position while walking the /start steps."""

    def __init__(self) -> None:
        self.idx: int = 0
        self.partial: dict[str, object] = {}

    @property
    def done(self) -> bool:
        return self.idx >= len(_STEPS)

    def first_prompt(self) -> str:
        return INTRO + _STEPS[0][1]

    def current_prompt(self) -> str:
        if self.done:
            return DONE_MESSAGE
        return _STEPS[self.idx][1]

    def submit(self, text: str) -> tuple[bool, str]:
        """Feed a user reply. Returns (advanced, reply_text).

        On validation error the session stays on the same step and returns a
        human-readable message. On the final step `advanced=True, reply=DONE`.
        """
        if self.done:
            return False, DONE_MESSAGE
        field, _, validator = _STEPS[self.idx]
        try:
            value = validator(text)
        except ValueError as exc:
            return False, f"⚠️ {exc}"
        self.partial[field] = value
        self.idx += 1
        if self.done:
            return True, DONE_MESSAGE
        return True, _STEPS[self.idx][1]

    def settings(self) -> Settings:
        if not self.done:
            raise RuntimeError("config session not complete")
        return Settings(**self.partial)  # type: ignore[arg-type]


def save_settings(
    conn: sqlite3.Connection, chat_id: int, settings: Settings
) -> None:
    """Upsert the chat's configuration row."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO chats(chat_id, tz, pushes_per_day, active_start, active_end, tone, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET "
        "tz=excluded.tz, pushes_per_day=excluded.pushes_per_day, "
        "active_start=excluded.active_start, active_end=excluded.active_end, "
        "tone=excluded.tone",
        (
            chat_id,
            settings.tz,
            settings.pushes_per_day,
            settings.active_start,
            settings.active_end,
            settings.tone,
            now,
        ),
    )


def load_settings(
    conn: sqlite3.Connection, chat_id: int
) -> Settings | None:
    row = conn.execute(
        "SELECT tz, pushes_per_day, active_start, active_end, tone "
        "FROM chats WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if row is None:
        return None
    return Settings(
        tz=row["tz"],
        pushes_per_day=row["pushes_per_day"],
        active_start=row["active_start"],
        active_end=row["active_end"],
        tone=row["tone"],
    )


def settings_as_dict(s: Settings) -> dict:
    return asdict(s)
