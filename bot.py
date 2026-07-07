#!/usr/bin/env python3
"""teach-me-eng-bot — FSRS-driven vocab agent with an LLM chat fallback.

Split responsibilities:
  * llm.py           — OpenAI-compatible chat client (one-shot + health)
  * vocab.py         — vocabulary CRUD, FSRS rating, weighted selection
  * prompts.py       — tone templates + just-talk system prompt composer
  * config_flow.py   — /start state machine for per-chat settings
  * scheduler.py     — push planning, compose, log, APScheduler wrapper
  * db.py            — SQLite schema and connection

This file wires them to python-telegram-bot: commands, callbacks, plain-text
handling, and scheduler bootstrap.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import html
import io
import logging
import os
import random
import sqlite3
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fsrs import Rating
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import cloze as cloze_module
import config_flow
import db as db_module
import games as games_module
import irregular_verbs as irregular_module
import llm
import prompts
import scheduler as sched_module
import spelling
import sysinfo
import translator
import typed_drill
import vocab

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a friendly English tutor chatting casually with a learner. "
    "Use natural, everyday English. If they ask about grammar, vocabulary, "
    "or usage, explain briefly with a small example.",
)
# Comma- or whitespace-separated Telegram user IDs allowed to talk to the bot.
# Empty/unset means no restriction (all users allowed).
ALLOWED_USER_IDS: set[int] = {
    int(x)
    for x in os.getenv("ALLOWED_USER_IDS", "").replace(",", " ").split()
    if x.strip()
}

MAX_MSG_LEN = 4000    # Telegram hard limit is 4096; responses past this spill into new messages
MAX_HISTORY_MESSAGES = 41  # 1 system + 40 turns; older turns are dropped so the LLM context can't grow unbounded

IMPORT_PENDING_TTL = 300.0   # /import → upload window (seconds)
IMPORT_MAX_BYTES = 1_000_000  # cap on uploaded CSV size
IMPORT_MAX_ROWS = 5000        # cap on rows accepted per import

ROOT = Path(__file__).resolve().parent
LOGS_DIR = ROOT / "logs"
CONVS_DIR = LOGS_DIR / "convs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
CONVS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOGS_DIR / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
# httpx/telegram INFO logs leak the bot token in request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# --- Mutable module state (set in main / post_init) --------------------------

# Database connection is opened in main() and reused by every handler + the
# scheduler. sqlite3.connect is called with check_same_thread=False because
# APScheduler's AsyncIOScheduler fires jobs in the same asyncio loop thread as
# PTB, but also schedules them from threaded setup; see db.connect().
conn: sqlite3.Connection | None = None
runner: sched_module.PushRunner | None = None
app: Application | None = None

# Per-chat conversation history stored in memory (system + user + assistant).
histories: dict[int, list[dict]] = {}
# Per-chat path of the active transcript file (rotated on /start and /clear).
conv_paths: dict[int, Path] = {}
# Chats currently walking the /start config steps.
sessions: dict[int, config_flow.ConfigSession] = {}
# Tokens → (chat_id, word) for pending /tr "Add to vocab" buttons.
pending_vocab: translator.PendingVocab = translator.PendingVocab()
# Chats that recently issued /import: chat_id → expiry monotonic timestamp.
# The next document upload from these chats is parsed as a vocab CSV.
import_pending: dict[int, float] = {}
# In-flight /games sessions: at most one per chat (AC8). Cleared on completion
# and on bot restart — game state is intentionally not persisted.
games: dict[int, games_module.Game] = {}
# In-flight /irregulars sessions: same one-per-chat semantics as `games`.
irregulars: dict[int, irregular_module.Game] = {}
# In-flight typed drills (Repeat / Focus drill — Game.kind tells them apart):
# same one-per-chat semantics as `games`.
typed_drills: dict[int, typed_drill.Game] = {}
# In-flight daily cloze-story sessions: one per chat, replaced by the next
# day's dispatch and cleared on completion, /games cancel, or bot restart.
cloze_sessions: dict[int, cloze_module.Session] = {}
# Label spec captured between `/games <spec>` and the direction-picker tap, as
# `(mode, names)` so OR-mode focus survives the round-trip. Missing entry means
# "no filter". Latest write wins; popped on game start.
pending_game_filters: dict[int, tuple[Literal["all", "any"], list[str]]] = {}
# Chats whose daily session dispatch found a typed game in flight; fired as
# soon as that game completes or is cancelled (see _fire_deferred_session).
deferred_sessions: set[int] = set()

GAMES_NEED_VOCAB = "add at least 4 words to your vocab first"
GAMES_IN_PROGRESS = "you have a game in progress"
GAMES_NO_LABEL_MATCH = (
    "no words match those labels — try fewer filters or `/label` more words"
)
GAMES_CANCELLED = "🛑 game cancelled"
GAMES_NOTHING_TO_CANCEL = "no game in progress"
IRREGULARS_IN_PROGRESS = "you have an irregular-verbs game in progress"
IRREGULARS_PROMPT_HINT = (
    'Reply with the past simple and past participle, e.g. "went / gone".'
)
DRILL_IN_PROGRESS = "you have a typed drill in progress"
DRILL_NOT_ENOUGH = (
    f"not enough words yet — add more or widen /focus "
    f"(need at least {typed_drill.MIN_ROUNDS})"
)
DRILL_PROMPT_HINT = "Type the translation."
GAMES_MENU_OUTDATED = "that game menu is outdated — send /games again"
STORY_IN_PROGRESS = (
    "you have an unfinished daily story — answer its blanks first, "
    "or /games cancel to abandon it"
)


def _in_progress_reply(chat_id: int) -> str | None:
    """The in-progress message for the chat's active game/session, or None.

    Single source of truth for every "is something already running?" gate —
    covers ALL state maps so a new activity can't slip past (the cloze
    session was originally missing from the hand-rolled per-handler chains).
    """
    for store, reply in (
        (games, GAMES_IN_PROGRESS),
        (irregulars, IRREGULARS_IN_PROGRESS),
        (typed_drills, DRILL_IN_PROGRESS),
        (cloze_sessions, STORY_IN_PROGRESS),
    ):
        if chat_id in store:
            return reply
    return None


def _typed_game_active(chat_id: int) -> bool:
    """True while a game that consumes plain-text answers is in flight."""
    return chat_id in irregulars or chat_id in typed_drills


# --- transcript + access helpers --------------------------------------------


def fresh_history(system_prompt: str = SYSTEM_PROMPT) -> list[dict]:
    return [{"role": "system", "content": system_prompt}]


def trim_history(history: list[dict], max_messages: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Keep the system message plus the most recent turns within the cap."""
    if len(history) <= max_messages:
        return history
    return [history[0]] + history[-(max_messages - 1):]


def start_conversation(chat_id: int) -> Path:
    """Open a fresh transcript file for this chat and register it as active."""
    chat_dir = CONVS_DIR / str(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)
    existing = [int(p.stem) for p in chat_dir.glob("*.txt") if p.stem.isdigit()]
    path = chat_dir / f"{max(existing, default=0) + 1:03d}.txt"
    path.touch()
    conv_paths[chat_id] = path
    log.info("New conversation file for chat %s: %s", chat_id, path)
    return path


def append_turn(chat_id: int, role: str, content: str) -> None:
    """Append one turn to the chat's active transcript, creating it on demand."""
    path = conv_paths.get(chat_id) or start_conversation(chat_id)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {role}: {content}\n\n")


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    if user is None or user.id not in ALLOWED_USER_IDS:
        log.warning(
            "Rejected message from unauthorized user id=%s username=%s",
            getattr(user, "id", None),
            getattr(user, "username", None),
        )
        return False
    return True


# --- telegram send helpers --------------------------------------------------


async def safe_send(
    bot,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Send a new Telegram message, retrying once on RetryAfter."""
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        return await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode,
            reply_markup=reply_markup,
        )


def split_point(text: str, max_len: int) -> int:
    """Return an index to split `text` so the head fits within `max_len`."""
    if len(text) <= max_len:
        return len(text)
    floor = max_len // 2
    for sep, offset in (("\n\n", 2), ("\n", 1), (". ", 2), ("! ", 2), ("? ", 2), (" ", 1)):
        idx = text.rfind(sep, floor, max_len)
        if idx != -1:
            return idx + offset
    return max_len


def _chunk_lines(lines: list[str], max_len: int, header: str) -> list[str]:
    """Pack `lines` into chunks where each chunk fits within `max_len`.

    The first chunk is prefixed with `header` (counted in the budget); later
    chunks contain only line content. Lines are never split mid-line: a single
    line longer than `max_len` is emitted on its own chunk regardless. With an
    empty `lines` the result is a single chunk holding just the header.
    """
    chunks: list[str] = []
    current = header
    for line in lines:
        sep_len = 1 if current else 0  # the joining "\n"
        if current and len(current) + sep_len + len(line) > max_len:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    chunks.append(current)
    return chunks


# --- push dispatch (called by the scheduler) --------------------------------


async def _push_llm_chat(messages: list[dict]) -> str:
    """llm.chat with reasoning disabled, for push/explain snippets.

    The openrouter/free auto-router sometimes lands on reasoning models whose
    chain-of-thought either eats the small max_tokens budget (content: null)
    or is returned as the content itself, leaking "We need to insert the
    word..." traces into user-facing pushes. Same class of bug as #100.
    """
    return await llm.chat(messages, disable_reasoning=True)


async def dispatch_push(chat_id: int) -> None:
    """Compose the daily cloze-story session and send its opening message."""
    assert conn is not None and app is not None
    if _typed_game_active(chat_id):
        # A typed game owns plain-text answers right now — installing the
        # session would hijack them mid-round. Fire when the game ends.
        deferred_sessions.add(chat_id)
        log.info("Session for chat %s deferred until the typed game ends", chat_id)
        return
    settings = config_flow.load_settings(conn, chat_id)
    if settings is None:
        log.info("No session for chat %s (chat unconfigured)", chat_id)
        return
    focus_text = vocab.get_focus_spec(conn, chat_id)
    mode, names = vocab.split_focus_spec(focus_text)
    names = names or None
    try:
        # Clamp for rows configured under the old 6-12 pushes/day range —
        # a 12-word story overwhelms both the learner and the free model.
        n_words = min(settings.words_per_day, config_flow.MAX_WORDS)
        session = await sched_module.compose_session(
            conn, chat_id, llm_chat=_push_llm_chat,
            n_words=n_words,
            names=names, mode=mode, rng=random.Random(),
        )
    except Exception as e:  # noqa: BLE001 — never let a session crash the scheduler
        log.error("compose_session failed for chat %s: %s", chat_id, e)
        return
    if session is None:
        log.info(
            "No session for chat %s (no vocab%s)",
            chat_id,
            f" under focus {focus_text!r}" if names else "",
        )
        return

    # Insert push_log first so the session can reference a real push id;
    # update tg_message_id after the Telegram send returns.
    push_id = sched_module.log_push(
        conn, chat_id, tg_message_id=None,
        word_ids=[b.word_id for b in session.blanks],
    )
    session.push_id = push_id
    body = cloze_module.format_session_message(session)
    try:
        msg = await app.bot.send_message(
            chat_id=chat_id, text=body, parse_mode="HTML"
        )
        conn.execute(
            "UPDATE push_log SET tg_message_id = ? WHERE id = ?",
            (msg.message_id, push_id),
        )
        cloze_sessions[chat_id] = session
        sched_module.save_session_json(
            conn, push_id, cloze_module.session_to_json(session)
        )
        append_turn(
            chat_id,
            "push",
            f"[story: {', '.join(b.word for b in session.blanks)}] {session.story}",
        )
    except Exception as e:  # noqa: BLE001
        log.error("Failed to send session to chat %s: %s", chat_id, e)
        # Drop the phantom row — leaving it would make sent_today() treat the
        # failed send as delivered and suppress any replan for the day.
        sched_module.delete_push(conn, push_id)


async def _fire_deferred_session(chat_id: int) -> None:
    """Dispatch a session that was deferred because a typed game was running."""
    if chat_id in deferred_sessions and not _typed_game_active(chat_id):
        deferred_sessions.discard(chat_id)
        await dispatch_push(chat_id)


# --- command handlers -------------------------------------------------------


# Single source of truth for commands — used by /help and set_my_commands.
COMMANDS: list[tuple[str, str]] = [
    ("start", "Configure schedule: timezone, words per daily story, active window, tone, target language"),
    ("help", "Show this help message"),
    ("add", "Add a word or phrase to this chat's vocab"),
    ("remove", "Remove a word or phrase from vocab"),
    ("list", "List vocab words (every row shows its labels; optional label spec filter, AND across tokens; prepend --any for OR)"),
    ("import", "Bulk-import vocab from a CSV file (one word per row)"),
    ("export", "Download this chat's vocab as a CSV file"),
    ("resetvocab", "Wipe this chat's vocabulary (with confirm)"),
    ("tr", "Translate args; tap the button under the reply to add the English word/phrase to vocab"),
    ("games", "Pick a game: Word->Translation / Translation->Word (vocab quiz, optional label filter), Irregular verbs, or Typed drill (/focus words + remembered mastery checks). /games cancel ends an in-flight game or story."),
    ("label", "Attach labels to a vocab word (e.g. /label horse pos:noun type:animal)"),
    ("unlabel", "Detach labels from a vocab word"),
    ("labels", "List every label in this chat with its attached-word count"),
    ("focus", "Set a sticky label spec scoping pushes + post-/forgot game button (e.g. /focus pos:noun, AND across tokens; prepend --any for OR, e.g. /focus --any type:body type:medicine); /focus clear removes it; /focus echoes current"),
    ("top", "Show learning progress within the current /focus spec: in-progress words sorted by score (0.0-3.0+) descending, plus separate Forgotten (focus:hard) and Remembered sections"),
    ("clear", "Reset the chat history (LLM memory)"),
    ("status", "Show host diagnostics, vocab/labels/focus, and LLM backend usage"),
]

HELP_TEXT = (
    "🤖 *Gemma vocab agent*\n\n"
    "*Getting started*\n"
    "1. Run /start and answer five questions: timezone, words per daily "
    "story (4–10), active window (HH:MM–HH:MM), tone, target language for /tr.\n"
    "2. Add words with /add <word or phrase> — or bulk-load a CSV with "
    "/import (and grab a backup any time with /export). Once a day, inside "
    "your window, the bot sends a short story with your words replaced by "
    "numbered blanks (plus a word bank).\n"
    "3. Type the missing word for each blank (or `skip` to give up on one) — "
    "every answer rates the word via FSRS spaced repetition, so tougher "
    "words come back more often. At the end you get the full story, your "
    "score, and translations of what you missed.\n"
    "4. Optional: tag words with /label (e.g. `/label horse pos:noun "
    "type:animal`) and slice the deck with /list, /games, or /focus.\n\n"
    "Plain (non-slash) messages chat with the model and will naturally reuse "
    "your vocab when it fits.\n\n"
    "*Commands*\n"
    + "\n".join(f"/{name} — {desc}" for name, desc in COMMANDS)
    + "\n\nLabel syntax + filters: "
    "https://github.com/valdisd96/teach-me-eng-bot#labels"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    session = config_flow.ConfigSession()
    sessions[chat_id] = session
    await update.message.reply_text(session.first_prompt())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    histories[chat_id] = fresh_history()
    start_conversation(chat_id)
    await update.message.reply_text("Conversation cleared.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id

    hardware = sysinfo.read_hardware()
    os_name = sysinfo.read_os_release()
    load1, load5, load15 = sysinfo.read_loadavg()
    temp = sysinfo.read_temperature()
    free, total = sysinfo.read_disk_free("/")
    deploy_sha = sysinfo.read_deploy_sha() or "unknown"
    words = vocab.count_words(conn, chat_id)
    labels = vocab.count_labels(conn, chat_id)
    focus_spec = vocab.get_focus_spec(conn, chat_id) or "none"
    server = await llm.health()
    usage_line = await llm.usage()

    await update.message.reply_text(
        "System\n"
        f"  Hardware: {hardware}\n"
        f"  OS: {os_name}\n"
        f"  Version: {deploy_sha}\n"
        f"  Load: {load1:.2f} {load5:.2f} {load15:.2f}\n"
        f"  Temp: {temp}\n"
        f"  Disk /: {sysinfo.format_bytes(free)} free / "
        f"{sysinfo.format_bytes(total)}\n"
        "\n"
        "Vocab\n"
        f"  Words: {words}\n"
        f"  Labels: {labels}\n"
        f"  Focus: {focus_spec}\n"
        "\n"
        "Model\n"
        f"  Server: {server}\n"
        f"  Usage: {usage_line}"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("Usage: /add <word or phrase>")
        return
    normalized = text.strip().lower()
    # Spell-check new single words before they enter the vocab — a typo here
    # gets force-woven into every future story. Dictionary only, no LLM
    # (issue #101); any checker failure falls through to a normal add.
    if vocab.find_word_id(conn, chat_id, normalized) is None:
        try:
            suggestion = await asyncio.to_thread(spelling.suggest, normalized)
        except Exception as e:  # noqa: BLE001
            log.warning("spell-check failed for %r: %s", normalized, e)
            suggestion = None
        if suggestion:
            fixed_token = pending_vocab.register(chat_id, suggestion)
            asis_token = pending_vocab.register(chat_id, normalized)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ Add “{suggestion}”", callback_data=f"av:{fixed_token}"
                )],
                [InlineKeyboardButton(
                    f"Add “{normalized}” anyway", callback_data=f"av:{asis_token}"
                )],
            ])
            await update.message.reply_text(
                f'🤔 "{normalized}" isn\'t in my dictionary — '
                f'did you mean "{suggestion}"?',
                reply_markup=kb,
            )
            return
    try:
        added = vocab.add_word(conn, chat_id, text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    if added:
        # Eagerly translate so future games never need a network call.
        # Failure here mustn't block the user-facing reply — backfill on next
        # bot start will pick up any rows still NULL.
        settings = config_flow.load_settings(conn, chat_id)
        target = settings.target_lang if settings is not None else "ru"
        try:
            translation = await asyncio.to_thread(
                translator.translate, normalized, target
            )
            vocab.set_translation(conn, chat_id, normalized, translation)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "cmd_add: translate %r → %r failed: %s", normalized, target, e
            )
        await update.message.reply_text(f"➕ Added: {normalized}")
    else:
        await update.message.reply_text(f"Already in your vocab: {normalized}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("Usage: /remove <word or phrase>")
        return
    removed = vocab.remove_word(conn, chat_id, text)
    normalized = text.strip().lower()
    if removed:
        await update.message.reply_text(f"➖ Removed: {normalized}")
    else:
        await update.message.reply_text(f"Not found: {normalized}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    args = context.args or []

    mode: Literal["all", "any"] = "all"
    rest = args
    if args and args[0].strip().lower() == vocab.FOCUS_ANY_FLAG:
        mode = "any"
        rest = args[1:]
        if not rest:
            await update.message.reply_text(
                f"⚠️ malformed label spec: {vocab.FOCUS_ANY_FLAG}"
            )
            return

    if rest:
        try:
            names = vocab.parse_label_spec(rest)
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        rows = vocab.words_matching_labels(conn, chat_id, names, mode=mode)
        if not rows:
            await update.message.reply_text("no words match those labels")
            return
        spec_text = (
            f"{vocab.FOCUS_ANY_FLAG} {' '.join(names)}" if mode == "any" else " ".join(names)
        )
        header = f"Vocab ({len(rows)}) matching {spec_text}:"
    else:
        rows = vocab.list_words(conn, chat_id)
        if not rows:
            await update.message.reply_text(
                "Your vocab is empty. Add words with /add <word>."
            )
            return
        header = f"Vocab ({len(rows)}):"

    label_map = vocab.labels_for_words_in_chat(conn, chat_id)
    scores = vocab.compute_scores(
        rows, hard_word_ids=vocab.hard_focus_word_ids(conn, chat_id)
    )
    lines = [
        f"• {r['text']} (seen {r['mention_count']}×, score {s})"
        + (
            f" — {', '.join(labels)}"
            if (labels := label_map.get(r["id"]))
            else ""
        )
        for r, s in zip(rows, scores)
    ]
    for chunk in _chunk_lines(lines, MAX_MSG_LEN, header):
        await update.message.reply_text(chunk)


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    import_pending[chat_id] = time.monotonic() + IMPORT_PENDING_TTL
    await update.message.reply_text(
        "Send me a CSV file to import.\n"
        "• One word or phrase per row, in the first column.\n"
        "• Optional second column `translation` is honoured when the header "
        "is `text,translation`; missing translations get backfilled "
        "automatically on next start.\n"
        "• Optional third column `labels` (header `text,translation,labels`) "
        "is a `;`-separated list of label names — round-trips with /export.\n"
        "• A bare `text` header (or no header) still works.\n"
        "• Existing words are kept; duplicates are skipped.\n"
        f"(Times out in {int(IMPORT_PENDING_TTL // 60)} minutes; "
        f"max {IMPORT_MAX_ROWS} rows / {IMPORT_MAX_BYTES // 1000} KB.)"
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    rows = vocab.list_words(conn, chat_id)
    if not rows:
        await update.message.reply_text(
            "Your vocab is empty. Add words with /add or /import."
        )
        return
    label_map = vocab.labels_for_words_in_chat(conn, chat_id)
    csv_text = vocab.format_csv(
        [(r["text"], r["translation"], label_map.get(r["id"], [])) for r in rows]
    )
    today = datetime.date.today().isoformat()
    filename = f"vocab-{today}.csv"
    await update.message.reply_document(
        document=io.BytesIO(csv_text.encode("utf-8")),
        filename=filename,
        caption=f"{len(rows)} words",
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a document upload as a CSV-vocab import.

    Only fires when the chat recently issued `/import` and the pending entry
    has not expired — uploads outside that window are ignored silently so the
    handler doesn't catch unrelated files.
    """
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    expiry = import_pending.pop(chat_id, None)
    if expiry is None or expiry < time.monotonic():
        return

    document = update.message.document
    if document is None:
        return
    if document.file_size and document.file_size > IMPORT_MAX_BYTES:
        await update.message.reply_text(
            f"⚠️ File too large ({document.file_size} bytes; "
            f"max {IMPORT_MAX_BYTES})."
        )
        return

    file = await context.bot.get_file(document.file_id)
    blob = await file.download_as_bytearray()
    if len(blob) > IMPORT_MAX_BYTES:
        await update.message.reply_text(
            f"⚠️ File too large ({len(blob)} bytes; max {IMPORT_MAX_BYTES})."
        )
        return
    try:
        # utf-8-sig also handles UTF-8-BOM-prefixed exports from Excel.
        text = bytes(blob).decode("utf-8-sig")
    except UnicodeDecodeError as e:
        await update.message.reply_text(f"⚠️ Could not decode file as UTF-8: {e}")
        return
    try:
        triples = vocab.parse_csv_words(text)
    except csv.Error as e:
        await update.message.reply_text(f"⚠️ Could not parse CSV: {e}")
        return
    if not triples:
        await update.message.reply_text("No words found in the file.")
        return
    if len(triples) > IMPORT_MAX_ROWS:
        await update.message.reply_text(
            f"⚠️ Too many rows ({len(triples)}; max {IMPORT_MAX_ROWS})."
        )
        return
    counts = vocab.import_rows(conn, chat_id, triples)
    parts = [
        f"added: {counts['added']}",
        f"skipped (duplicate): {counts['skipped']}",
    ]
    if counts["invalid"]:
        parts.append(f"skipped (empty): {counts['invalid']}")
    if counts["rejected"]:
        parts.append(f"rejected (labels): {counts['rejected']}")
    reply = "Imported. " + ", ".join(parts) + "."
    label_errors = counts["label_errors"]
    if label_errors:
        shown = label_errors[:3]
        detail = "; ".join(f"row {idx}: {msg}" for idx, msg in shown)
        more = (
            f" (+{len(label_errors) - len(shown)} more)"
            if len(label_errors) > len(shown)
            else ""
        )
        reply += f"\nLabel errors — {detail}{more}"
    await update.message.reply_text(reply)


async def cmd_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    settings = config_flow.load_settings(conn, chat_id)
    if settings is None:
        await update.message.reply_text(
            "Run /start first so I know which language to translate into.",
            do_quote=True,
        )
        return

    # Two input modes: explicit args, or a reply to another message.
    args_text = " ".join(context.args or []).strip()
    if args_text:
        source_text = args_text
    elif update.message.reply_to_message is not None:
        replied = update.message.reply_to_message
        source_text = (replied.text or replied.caption or "").strip()
    else:
        source_text = ""

    if not source_text:
        await update.message.reply_text(
            "Usage: /tr <text>, or reply to a message with /tr.",
            do_quote=True,
        )
        return

    reverse = translator.is_target_script(source_text, settings.target_lang)
    target_iso = "en" if reverse else settings.target_lang
    source_iso = settings.target_lang if reverse else "auto"

    try:
        translated = await asyncio.to_thread(
            translator.translate, source_text, target_iso, source_iso
        )
    except Exception as e:  # noqa: BLE001 — surface the reason to the user
        log.error("translate failed for chat %s: %s", chat_id, e)
        await update.message.reply_text(
            f"⚠️ Translation failed: {e}", do_quote=True
        )
        return

    if not translated:
        await update.message.reply_text("⚠️ Empty translation.", do_quote=True)
        return

    # Both directions can feed English into vocab (reverse adds the translation,
    # forward adds the source), but we defer the write until the user taps the
    # button. Long phrases still short-circuit with an inline note — same 5-word
    # cap on the user's input as before.
    to_add = translator.vocab_target(source_text, translated, reverse)
    word_count = len(source_text.split())
    if word_count > 5:
        note = translator.format_vocab_note(word_count, added=False)
        await update.message.reply_text(f"{translated}\n{note}", do_quote=True)
        return

    token = pending_vocab.register(chat_id, to_add)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Add to vocab", callback_data=f"av:{token}")]]
    )
    await update.message.reply_text(translated, reply_markup=kb, do_quote=True)


async def cmd_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /label <word> <spec> [<spec> ...]"
        )
        return
    word_arg, spec_args = args[0], args[1:]
    try:
        names = vocab.parse_label_spec(spec_args)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    reserved = sorted(set(names) & vocab.RESERVED_LABEL_NAMES)
    if reserved:
        await update.message.reply_text(
            f"⚠️ {', '.join(reserved)} is a system label — managed automatically, "
            "can't be set by hand"
        )
        return
    word_id = vocab.find_word_id(conn, chat_id, word_arg)
    normalized_word = word_arg.strip().lower()
    if word_id is None:
        await update.message.reply_text(f"Not found: {normalized_word}")
        return

    before = vocab.labels_for_word(conn, word_id)
    before_set = set(before)
    before_pos = next(
        (n for n in before if n.startswith(vocab.POS_PREFIX)), None
    )
    for name in names:
        lid = vocab.get_or_create_label(conn, chat_id, name)
        vocab.attach_label(conn, word_id, lid)
    after = vocab.labels_for_word(conn, word_id)
    after_set = set(after)
    after_pos = next(
        (n for n in after if n.startswith(vocab.POS_PREFIX)), None
    )

    parts: list[str] = []
    swap_target: str | None = None
    if before_pos and after_pos and before_pos != after_pos:
        parts.append(f"replaced {before_pos} → {after_pos}")
        swap_target = after_pos
    truly_added = sorted(
        n for n in after_set - before_set if n != swap_target
    )
    if truly_added:
        parts.append("added: " + ", ".join(truly_added))
    if not parts:
        parts.append("already attached: " + ", ".join(names))
    await update.message.reply_text(f"{normalized_word}: " + "; ".join(parts))


async def cmd_unlabel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /unlabel <word> <spec> [<spec> ...]"
        )
        return
    word_arg, spec_args = args[0], args[1:]
    try:
        names = vocab.parse_label_spec(spec_args)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    reserved = sorted(set(names) & vocab.RESERVED_LABEL_NAMES)
    if reserved:
        await update.message.reply_text(
            f"⚠️ {', '.join(reserved)} is a system label — managed automatically, "
            "can't be removed by hand"
        )
        return
    word_id = vocab.find_word_id(conn, chat_id, word_arg)
    normalized_word = word_arg.strip().lower()
    if word_id is None:
        await update.message.reply_text(f"Not found: {normalized_word}")
        return

    detached: list[str] = []
    for name in names:
        lid = vocab.find_label_id(conn, chat_id, name)
        if lid is None:
            continue
        if vocab.detach_label(conn, word_id, lid):
            detached.append(name)
    if detached:
        msg = "removed: " + ", ".join(sorted(detached))
    else:
        msg = "nothing to remove"
    await update.message.reply_text(f"{normalized_word}: {msg}")


async def cmd_labels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    rows = vocab.labels_with_counts(conn, chat_id)
    if not rows:
        await update.message.reply_text(
            "No labels yet. Use /label <word> <spec>… to add some."
        )
        return
    body = "\n".join(f"• {name} ({n})" for name, n in rows)
    await update.message.reply_text(f"Labels:\n{body}")


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set, clear, or echo the chat's sticky focus spec.

    Forms:
      * `/focus`                       → echo current focus or "no focus set".
      * `/focus clear`                 → clear the focus (column → NULL).
      * `/focus pos:noun ...`          → AND across labels.
      * `/focus --any pos:noun type:x` → OR across labels (leading flag).
    """
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        current = vocab.get_focus_spec(conn, chat_id)
        await update.message.reply_text(
            f"current focus: {current}" if current else "no focus set"
        )
        return

    if len(args) == 1 and args[0].strip().lower() == "clear":
        vocab.set_focus_spec(conn, chat_id, None)
        await update.message.reply_text("focus cleared")
        return

    mode: Literal["all", "any"] = "all"
    rest = args
    if args[0].strip().lower() == vocab.FOCUS_ANY_FLAG:
        mode = "any"
        rest = args[1:]
        if not rest:
            await update.message.reply_text(
                f"⚠️ malformed label spec: {vocab.FOCUS_ANY_FLAG}"
            )
            return

    try:
        names = vocab.parse_label_spec(rest)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    spec_text = (
        f"{vocab.FOCUS_ANY_FLAG} {' '.join(names)}" if mode == "any" else " ".join(names)
    )
    vocab.set_focus_spec(conn, chat_id, spec_text)
    matches = vocab.words_matching_labels(conn, chat_id, names, mode=mode)
    if not matches:
        await update.message.reply_text(
            f"focus set: {spec_text}\n⚠️ no words match yet"
        )
    else:
        await update.message.reply_text(
            f"focus set: {spec_text} ({len(matches)} word"
            f"{'s' if len(matches) != 1 else ''})"
        )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report learning progress within the chat's current /focus spec.

    Three sections: Top (in-progress, sorted by remembered_streak DESC, score
    shown), Forgotten (focus:hard label), Remembered (remembered label).
    """
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id

    focus_text = vocab.get_focus_spec(conn, chat_id)
    if not focus_text:
        await update.message.reply_text("no focus set — set one with /focus first")
        return

    mode, names = vocab.split_focus_spec(focus_text)
    rows = vocab.words_matching_labels(conn, chat_id, names, mode=mode)
    remembered_ids = vocab.remembered_word_ids(conn, chat_id)
    hard_ids = vocab.hard_focus_word_ids(conn, chat_id)

    remembered_rows = sorted(
        [r for r in rows if r["id"] in remembered_ids],
        key=lambda r: r["text"],
    )
    # Remembered wins over Forgotten if a word somehow carries both labels.
    forgotten_rows = sorted(
        [r for r in rows if r["id"] in hard_ids and r["id"] not in remembered_ids],
        key=lambda r: r["text"],
    )
    classified = remembered_ids | hard_ids
    top_rows = sorted(
        [r for r in rows if r["id"] not in classified],
        key=lambda r: (-r["remembered_streak"], r["text"]),
    )

    lines: list[str] = []
    lines.append(f"Top ({len(top_rows)}):")
    if top_rows:
        lines.extend(
            f"• {r['text']} — score {r['remembered_streak']:.1f}" for r in top_rows
        )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Forgotten ({len(forgotten_rows)}):")
    if forgotten_rows:
        lines.extend(f"• {r['text']}" for r in forgotten_rows)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Remembered ({len(remembered_rows)}):")
    if remembered_rows:
        lines.extend(f"• {r['text']}" for r in remembered_rows)
    else:
        lines.append("  (none)")

    for chunk in _chunk_lines(lines, MAX_MSG_LEN, ""):
        await update.message.reply_text(chunk)


async def cmd_resetvocab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Yes, wipe", callback_data="rv:yes"),
            InlineKeyboardButton("Cancel", callback_data="rv:no"),
        ]]
    )
    await update.message.reply_text(
        "Wipe all words for this chat? This cannot be undone.", reply_markup=kb
    )


# --- /games -----------------------------------------------------------------


def _playable_rows(
    conn_: sqlite3.Connection,
    chat_id: int,
    names: list[str] | None = None,
    *,
    mode: Literal["all", "any"] = "all",
) -> list[sqlite3.Row]:
    rows = (
        vocab.words_matching_labels(conn_, chat_id, names, mode=mode)
        if names
        else vocab.list_words(conn_, chat_id)
    )
    excluded = vocab.remembered_word_ids(conn_, chat_id)
    return [r for r in rows if r["translation"] and r["id"] not in excluded]


def _round_keyboard(game: games_module.Game) -> InlineKeyboardMarkup:
    rd = game.current()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"g:{game.current_round}:{i}")]
        for i, opt in enumerate(rd.options)
    ])


async def _send_round(bot, chat_id: int, game: games_module.Game) -> None:
    rd = game.current()
    text = f"Round {game.current_round + 1}/{game.n_rounds}: {rd.prompt}"
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=_round_keyboard(game))


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    spec_args = context.args or []
    # /games cancel — must run BEFORE the in-progress short-circuit, otherwise
    # GAMES_IN_PROGRESS would fire and the user could never cancel.
    if len(spec_args) == 1 and spec_args[0].strip().lower() == "cancel":
        had_game = _in_progress_reply(chat_id) is not None
        games.pop(chat_id, None)
        irregulars.pop(chat_id, None)
        typed_drills.pop(chat_id, None)
        cz = cloze_sessions.pop(chat_id, None)
        if cz is not None and cz.push_id is not None:
            # Close the push_log row so a restart doesn't resurrect the
            # cancelled story via session rehydration.
            sched_module.mark_rated(conn, cz.push_id)
        pending_game_filters.pop(chat_id, None)
        await update.message.reply_text(
            GAMES_CANCELLED if had_game else GAMES_NOTHING_TO_CANCEL
        )
        await _fire_deferred_session(chat_id)
        return
    in_progress = _in_progress_reply(chat_id)
    if in_progress:
        await update.message.reply_text(in_progress)
        return
    if spec_args:
        try:
            names = vocab.parse_label_spec(spec_args)
        except ValueError as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        if len(_playable_rows(conn, chat_id, names)) < games_module.MIN_VOCAB:
            await update.message.reply_text(GAMES_NO_LABEL_MATCH)
            return
        pending_game_filters[chat_id] = ("all", names)
        # With a spec, the picker is vocab-only — the spec is meaningless
        # for the static irregular-verb deck.
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Word → Translation", callback_data="gm:wt"),
            InlineKeyboardButton("Translation → Word", callback_data="gm:tw"),
        ]])
    else:
        pending_game_filters.pop(chat_id, None)
        rows = [
            [
                InlineKeyboardButton("Irregular verbs", callback_data="gm:irr"),
                InlineKeyboardButton("Typed drill", callback_data="gm:drill"),
            ],
        ]
        # Vocab buttons only when there's enough translatable vocab to play —
        # otherwise the irregular verbs game stays reachable from the same picker.
        if len(_playable_rows(conn, chat_id)) >= games_module.MIN_VOCAB:
            rows.insert(0, [
                InlineKeyboardButton("Word → Translation", callback_data="gm:wt"),
                InlineKeyboardButton("Translation → Word", callback_data="gm:tw"),
            ])
        kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text("Pick a game:", reply_markup=kb)


def _format_irregular_prompt(game: irregular_module.Game) -> str:
    rd = game.current()
    return (
        f"Round {game.current_round + 1}/{game.n_rounds}: {rd.base}\n"
        f"{IRREGULARS_PROMPT_HINT}"
    )


def _format_drill_prompt(game: typed_drill.Game) -> str:
    rd = game.current()
    return (
        f"Round {game.current_round + 1}/{game.n_rounds}: {rd.prompt}\n"
        f"{DRILL_PROMPT_HINT}"
    )


async def _send_drill_round(bot, chat_id: int, game: typed_drill.Game) -> None:
    await bot.send_message(chat_id=chat_id, text=_format_drill_prompt(game))


# --- callback handlers ------------------------------------------------------


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stub for the retired pre-story ✅/❌ push buttons.

    The multi-push format was replaced by the daily cloze story (8dea07a);
    taps on old messages just clear the buttons instead of rating anything.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer("this push format is retired — daily stories replaced it")
    try:
        await query.edit_message_reply_markup(None)
    except BadRequest:
        pass  # message too old or already replaced


async def on_explain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """💡 button under a story result's missed word — meaning + one example."""
    if not is_allowed(update):
        return
    assert conn is not None
    query = update.callback_query
    await query.answer()
    try:
        _, word_id_s = query.data.split(":")
        word_id = int(word_id_s)
    except (ValueError, AttributeError):
        log.warning("Malformed exp callback: %r", query.data)
        return
    chat_id = update.effective_chat.id
    try:
        explanation = await sched_module.compose_explanation(
            conn, word_id, llm_chat=_push_llm_chat
        )
    except Exception as e:  # noqa: BLE001 — surface a soft failure to the user
        log.error("explain failed for word %s: %s", word_id, e)
        explanation = None
    if not explanation:
        await query.message.reply_text(
            "⚠️ couldn't fetch an explanation right now — try again later"
        )
        return
    row = conn.execute(
        "SELECT text FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    word = row["text"] if row is not None else "?"
    await query.message.reply_text(
        f"💡 <b>{html.escape(word)}</b>\n{html.escape(explanation)}",
        parse_mode="HTML",
    )
    append_turn(chat_id, "explain", f"[{word}] {explanation}")


async def on_add_vocab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    query = update.callback_query
    await query.answer()
    try:
        _, token_s = query.data.split(":")
        token = int(token_s)
    except (ValueError, AttributeError):
        log.warning("Malformed av callback: %r", query.data)
        return
    entry = pending_vocab.pop(token)
    if entry is None:
        # Bot restart orphaned the token, or the registry evicted it.
        label = "⚠️ expired"
    else:
        entry_chat_id, word = entry
        try:
            added = vocab.add_word(conn, entry_chat_id, word)
        except ValueError:
            added = False
        label = "added to vocab ✅" if added else "already in vocab"
    try:
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data="noop")]]
            )
        )
    except BadRequest:
        pass  # message too old or already replaced


async def on_resetvocab_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    if query.data == "rv:yes":
        conn.execute("DELETE FROM words WHERE chat_id = ?", (chat_id,))
        await query.edit_message_text("Vocab wiped.")
    else:
        await query.edit_message_text("Cancelled.")


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Already-rated button press — just acknowledge so the spinner clears.
    if update.callback_query:
        await update.callback_query.answer()


async def on_games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    in_progress = _in_progress_reply(chat_id)
    if in_progress:
        await query.message.reply_text(in_progress)
        return
    if query.data == "gm:irr":
        rounds = irregular_module.draw_rounds(rng=random.Random())
        game = irregular_module.Game(chat_id=chat_id, rounds=rounds)
        irregulars[chat_id] = game
        await query.message.reply_text(_format_irregular_prompt(game))
        return
    if query.data == "gm:drill":
        focus_text = vocab.get_focus_spec(conn, chat_id)
        mode, names_list = vocab.split_focus_spec(focus_text)
        names = names_list or None
        focus_pool = _playable_rows(conn, chat_id, names, mode=mode)
        remembered_ids = vocab.remembered_word_ids(conn, chat_id)
        mastered_ids = vocab.mastered_word_ids(conn, chat_id)
        salt_pool = [
            r for r in vocab.list_words(conn, chat_id)
            if r["id"] in remembered_ids and r["id"] not in mastered_ids
        ]
        try:
            rounds = typed_drill.draw_rounds(
                focus_pool, salt_pool, rng=random.Random()
            )
        except ValueError:
            await query.message.reply_text(DRILL_NOT_ENOUGH)
            return
        game = typed_drill.Game(chat_id=chat_id, rounds=rounds)
        typed_drills[chat_id] = game
        await _send_drill_round(context.bot, chat_id, game)
        return
    if query.data == "gm:wt":
        direction = "wt"
    elif query.data == "gm:tw":
        direction = "tw"
    else:
        # Includes gm:repeat / gm:focus buttons on menus sent before the
        # single Typed-drill button replaced them.
        log.warning("Outdated/malformed gm callback: %r", query.data)
        await query.message.reply_text(GAMES_MENU_OUTDATED)
        return
    stash = pending_game_filters.get(chat_id)
    if stash:
        mode, names = stash
    else:
        mode, names = "all", None
    rows = _playable_rows(conn, chat_id, names, mode=mode)
    if len(rows) < games_module.MIN_VOCAB:
        await query.message.reply_text(
            GAMES_NO_LABEL_MATCH if names else GAMES_NEED_VOCAB
        )
        return
    rounds = games_module.draw_rounds(rows, direction=direction, rng=random.Random())
    game = games_module.Game(chat_id=chat_id, rounds=rounds)
    games[chat_id] = game
    pending_game_filters.pop(chat_id, None)
    await _send_round(context.bot, chat_id, game)


async def on_game_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    query = update.callback_query
    await query.answer()
    try:
        _, round_idx_s, chosen_idx_s = query.data.split(":")
        round_idx = int(round_idx_s)
        chosen_idx = int(chosen_idx_s)
    except (ValueError, AttributeError):
        log.warning("Malformed game callback: %r", query.data)
        return
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if game is None or game.done:
        return  # stale tap after restart or post-completion
    if round_idx != game.current_round:
        return  # stale tap from an earlier round
    rd = game.current()
    correct = chosen_idx == rd.correct_index

    feedback_rows = []
    for i, opt in enumerate(rd.options):
        if i == chosen_idx and correct:
            label = f"✅ {opt}"
        elif i == chosen_idx and not correct:
            label = f"❌ {opt}"
        elif not correct and i == rd.correct_index:
            label = f"✅ {opt}"
        else:
            label = opt
        feedback_rows.append([InlineKeyboardButton(label, callback_data="noop")])

    games_module.apply_answer(game, chosen_idx)
    assert conn is not None
    try:
        vocab.record_outcome(
            conn, rd.word_id, correct=correct, weight=0.5, source="game"
        )
    except KeyError:
        log.warning("record_outcome: unknown word_id %s", rd.word_id)

    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(feedback_rows))
    except BadRequest:
        pass  # message too old or already edited

    if game.done:
        await safe_send(
            context.bot,
            chat_id,
            games_module.format_result(game.score, game.n_rounds),
        )
        games.pop(chat_id, None)
    else:
        await _send_round(context.bot, chat_id, game)


# --- plain-text handler: config session OR just-talk ------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None

    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()
    if not user_text:
        return

    # Branch 1: user is mid-/start configuration.
    session = sessions.get(chat_id)
    if session is not None:
        _, reply = session.submit(user_text)
        if session.done:
            settings = session.settings()
            config_flow.save_settings(conn, chat_id, settings)
            if runner is not None:
                runner.schedule_chat(chat_id, settings)
            sessions.pop(chat_id, None)
            log.info("Config saved for chat %s: %s", chat_id, settings)
        await update.message.reply_text(reply)
        return

    # Branch 1b: daily cloze-story session in progress — interpret plain text
    # as the answer for the current blank. Each answer applies the FSRS
    # rating the old ✅/❌ push buttons used to (correct → Good, miss → Again).
    cz = cloze_sessions.get(chat_id)
    if cz is not None and not cz.done:
        kind = cloze_module.classify_answer(user_text, cz)
        if kind == "other":
            # Not a word-bank word and not a skip — grading it would rate an
            # unrelated word `Again` off a stray chat message. Nudge instead.
            await update.message.reply_text(
                cloze_module.format_not_answer_hint(cz)
            )
            return
        blank = cz.current()
        correct = kind == "answer" and cloze_module.grade_answer(
            user_text, blank.word
        )
        try:
            vocab.rate_word(
                conn, blank.word_id, Rating.Good if correct else Rating.Again
            )
            vocab.record_outcome(
                conn, blank.word_id, correct=correct, weight=1.0, source="push"
            )
        except KeyError:
            log.warning("cloze answer: unknown word_id %s", blank.word_id)
        cloze_module.apply_answer(cz, correct)
        if cz.push_id is not None:
            sched_module.save_session_json(
                conn, cz.push_id, cloze_module.session_to_json(cz)
            )
        await update.message.reply_text(
            cloze_module.format_answer_feedback(correct, blank.word)
        )
        if cz.done:
            if cz.push_id is not None:
                sched_module.mark_rated(conn, cz.push_id)
            result = cloze_module.format_result(cz)
            # Missed words get a 💡 explain button each — the story-session
            # replacement for the old ❌-forgot explanation follow-up.
            missed = [b for b in cz.blanks if b.word in cz.wrong]
            kb = (
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                f"💡 {b.word}", callback_data=f"exp:{b.word_id}"
                            )
                        ]
                        for b in missed
                    ]
                )
                if missed
                else None
            )
            await safe_send(
                context.bot, chat_id, result, parse_mode="HTML", reply_markup=kb
            )
            append_turn(
                chat_id, "story-result", f"{cz.score}/{cz.n_blanks}"
            )
            cloze_sessions.pop(chat_id, None)
        else:
            await update.message.reply_text(
                cloze_module.format_blank_prompt(cz)
            )
        return

    # Branch 2: irregular-verbs game in progress — interpret plain text as the
    # round answer.
    irr_game = irregulars.get(chat_id)
    if irr_game is not None and not irr_game.done:
        rd = irr_game.current()
        correct, parsed = irregular_module.grade_answer(user_text, rd)
        canonical = (
            f"{rd.past_simple_alts[0]} / {rd.past_participle_alts[0]}"
        )
        if parsed is None:
            await update.message.reply_text(
                f"⚠️ {IRREGULARS_PROMPT_HINT}"
            )
            return
        if correct:
            reply = f"✅ {parsed}"
        else:
            reply = f"❌ {parsed} — correct: {canonical}"
        irregular_module.apply_answer(irr_game, correct)
        await update.message.reply_text(reply)
        if irr_game.done:
            await update.message.reply_text(
                irregular_module.format_result(irr_game.score, irr_game.n_rounds)
            )
            irregulars.pop(chat_id, None)
            await _fire_deferred_session(chat_id)
        else:
            await update.message.reply_text(_format_irregular_prompt(irr_game))
        return

    # Branch 2b: typed drill in progress — interpret plain text as the round
    # answer. Salted remembered rounds (rd.judged) grade via the tolerant
    # LLM judge and record source="repeat" (gates the forget-flip: a wrong
    # answer drops `remembered` and attaches `focus:hard`); regular focus
    # rounds grade strictly and record source="game" (no forget-flip —
    # those words are non-remembered by construction).
    drill = typed_drills.get(chat_id)
    if drill is not None and not drill.done:
        rd = drill.current()
        judged = True
        if rd.judged:
            verdict = await typed_drill.grade_answer_llm(user_text, rd)
            # None = judge unavailable: score as strict-wrong, but record with
            # source="game" so the miss can't demote a remembered word.
            judged = verdict is not None
            correct = bool(verdict)
        else:
            correct = typed_drill.grade_answer(user_text, rd)
        if correct:
            reply = f"✅ {rd.expected}"
        else:
            reply = f"❌ correct: {rd.expected}"
        source_word = rd.prompt if rd.direction == "en2ru" else rd.expected
        typed_drill.apply_answer(drill, correct, source_word=source_word)
        try:
            vocab.record_outcome(
                conn, rd.word_id, correct=correct, weight=0.5,
                source="repeat" if rd.judged and judged else "game",
            )
        except KeyError:
            log.warning("record_outcome: unknown word_id %s", rd.word_id)
        await update.message.reply_text(reply)
        if drill.done:
            await update.message.reply_text(
                typed_drill.format_result(
                    drill.score, drill.n_rounds, drill.wrong
                )
            )
            typed_drills.pop(chat_id, None)
            await _fire_deferred_session(chat_id)
        else:
            await update.message.reply_text(_format_drill_prompt(drill))
        return

    # Branch 3: just-talk. Inject vocab words into the system prompt so the
    # model uses them when they fit naturally; scan the reply afterwards to
    # bump mention_count.
    vocab_rows = vocab.list_words(conn, chat_id)
    word_texts = [r["text"] for r in vocab_rows]
    system = prompts.just_talk_system(SYSTEM_PROMPT, word_texts)

    if chat_id not in histories:
        histories[chat_id] = fresh_history(system)
        start_conversation(chat_id)
    elif histories[chat_id] and histories[chat_id][0]["role"] == "system":
        # Refresh system prompt every turn so new/removed words take effect.
        histories[chat_id][0]["content"] = system

    histories[chat_id].append({"role": "user", "content": user_text})
    histories[chat_id] = trim_history(histories[chat_id])
    append_turn(chat_id, "user", user_text)

    # One-shot reply (the token-streaming live-edit machinery served a
    # feature used once in months — a typing indicator + full reply is
    # simpler and rate-limit-proof).
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        reply = await llm.chat(
            histories[chat_id], max_tokens=1024, temperature=0.7
        )
    except Exception as e:  # noqa: BLE001 — surface the failure to the user
        log.error("just-talk chat error: %s", e)
        append_turn(chat_id, "error", f"{type(e).__name__}: {e}")
        await update.message.reply_text(f"⚠️ Error: {e}")
        return

    final_text = reply or "⚠️ No response from model."
    remaining = final_text
    while remaining:
        idx = split_point(remaining, MAX_MSG_LEN)
        head, remaining = remaining[:idx], remaining[idx:]
        await safe_send(
            context.bot, chat_id,
            vocab.highlight_matches(head, word_texts),
            parse_mode="HTML",
        )

    histories[chat_id].append({"role": "assistant", "content": reply})
    append_turn(chat_id, "assistant", reply)

    # Count any vocab words literally present in the reply.
    id_pairs = [(r["id"], r["text"]) for r in vocab_rows]
    mentioned = vocab.scan_mentions(reply, id_pairs)
    if mentioned:
        vocab.bump_mentions(conn, mentioned)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Central PTB error handler: log everything, tell the user something.

    Without one, an exception escaping any handler is only swallowed by
    PTB's "No error handlers are registered" fallback and the user gets
    dead silence. Polling-level network errors arrive with update=None and
    are just logged.
    """
    log.error("Unhandled exception in handler", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return
    try:
        await context.bot.send_message(
            chat_id=chat.id, text="⚠️ something went wrong — please try again"
        )
    except Exception:  # noqa: BLE001 — never raise from the error handler
        log.exception("Failed to notify chat %s about an error", chat.id)


# --- bootstrap --------------------------------------------------------------


async def _post_init(application: Application) -> None:
    """Start APScheduler and reschedule jobs for every known chat.

    Called by PTB after the asyncio loop is running, so AsyncIOScheduler can
    attach to it cleanly.
    """
    global runner
    assert conn is not None
    runner = sched_module.PushRunner(conn, dispatch=dispatch_push)
    runner.start()
    runner.refresh_all()
    log.info("Scheduler started; refreshed jobs for all known chats.")
    # Rehydrate any daily session that was in flight when the bot restarted —
    # without this, deploys strand the day's story (typed answers would fall
    # through to just-talk and the blanks could never be answered).
    for row in conn.execute("SELECT chat_id, tz FROM chats").fetchall():
        payload = sched_module.load_unfinished_session_json(
            conn, row["chat_id"], row["tz"]
        )
        if payload is None:
            continue
        try:
            cloze_sessions[row["chat_id"]] = cloze_module.session_from_json(payload)
            log.info("Rehydrated in-flight session for chat %s", row["chat_id"])
        except (ValueError, TypeError, KeyError) as e:
            log.warning(
                "Could not rehydrate session for chat %s: %s", row["chat_id"], e
            )
    # One-shot translation backfill for rows added before the column existed
    # (or whose previous translate attempt failed). Network-bound, so offload.
    counts = await asyncio.to_thread(
        vocab.backfill_translations,
        conn,
        translate_fn=translator.translate,
        log=log,
    )
    if counts["translated"] or counts["failed"]:
        log.info(
            "Translation backfill: translated=%d failed=%d",
            counts["translated"], counts["failed"],
        )
    # Populate Telegram's native command menu so clients show autocomplete.
    await application.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc in COMMANDS]
    )


async def _post_shutdown(application: Application) -> None:
    if runner is not None:
        runner.stop()


def main() -> None:
    global conn, app

    conn = db_module.connect()
    db_module.init_db(conn)

    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30)
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("resetvocab", cmd_resetvocab))
    app.add_handler(CommandHandler("tr", cmd_translate))
    app.add_handler(CommandHandler("games", cmd_games))
    app.add_handler(CommandHandler("label", cmd_label))
    app.add_handler(CommandHandler("unlabel", cmd_unlabel))
    app.add_handler(CommandHandler("labels", cmd_labels))
    app.add_handler(CommandHandler("focus", cmd_focus))
    app.add_handler(CommandHandler("top", cmd_top))

    app.add_handler(CallbackQueryHandler(on_rate, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(on_resetvocab_confirm, pattern=r"^rv:"))
    app.add_handler(CallbackQueryHandler(on_add_vocab, pattern=r"^av:"))
    app.add_handler(CallbackQueryHandler(on_games_menu, pattern=r"^gm:"))
    app.add_handler(CallbackQueryHandler(on_explain, pattern=r"^exp:"))
    app.add_handler(CallbackQueryHandler(on_game_answer, pattern=r"^g:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
