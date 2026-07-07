#!/usr/bin/env python3
"""teach-me-eng-bot — streaming chat + FSRS-driven vocab agent.

Split responsibilities:
  * llm.py           — OpenAI-compatible chat client (stream + one-shot + health)
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

import config_flow
import db as db_module
import focus_drill as focus_drill_module
import games as games_module
import irregular_verbs as irregular_module
import llm
import prompts
import repeat_game as repeat_module
import scheduler as sched_module
import sysinfo
import translator
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

CURSOR = "▌"
EDIT_INTERVAL = 2.0   # seconds between Telegram message edits (rate-limit safe)
MAX_MSG_LEN = 4000    # Telegram hard limit is 4096; responses past this spill into new messages

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
# In-flight "Repeat (typed)" sessions: same one-per-chat semantics as `games`.
repeat_games: dict[int, repeat_module.Game] = {}
# In-flight "Focus drill (typed)" sessions: same one-per-chat semantics.
focus_drills: dict[int, focus_drill_module.Game] = {}
# Label spec captured between `/games <spec>` (or focus seed via on_play_game)
# and the direction-picker tap, as `(mode, names)` so OR-mode focus survives the
# round-trip. Missing entry means "no filter". Latest write wins; popped on game start.
pending_game_filters: dict[int, tuple[Literal["all", "any"], list[str]]] = {}

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
REPEAT_IN_PROGRESS = "you have a repeat game in progress"
REPEAT_NOT_ENOUGH = (
    f"not enough remembered words yet — keep practising "
    f"(need at least {repeat_module.N_ROUNDS})"
)
REPEAT_PROMPT_HINT = "Type the translation."
FOCUS_DRILL_IN_PROGRESS = "you have a focus drill in progress"
FOCUS_DRILL_NOT_ENOUGH = (
    f"not enough focus words yet — add more or widen /focus "
    f"(need at least {focus_drill_module.MIN_ROUNDS})"
)
FOCUS_DRILL_PROMPT_HINT = "Type the translation."


# --- transcript + access helpers --------------------------------------------


def fresh_history(system_prompt: str = SYSTEM_PROMPT) -> list[dict]:
    return [{"role": "system", "content": system_prompt}]


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


async def safe_edit(message, text: str, *, parse_mode: str | None = None) -> None:
    """Edit a Telegram message, handling rate-limit and no-change errors."""
    try:
        await message.edit_text(text, parse_mode=parse_mode)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        await message.edit_text(text, parse_mode=parse_mode)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def safe_send(bot, chat_id: int, text: str, *, parse_mode: str | None = None):
    """Send a new Telegram message, retrying once on RetryAfter."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)


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
    """Compose a scheduled push and send it with rating buttons."""
    assert conn is not None and app is not None
    focus_text = vocab.get_focus_spec(conn, chat_id)
    mode, names = vocab.split_focus_spec(focus_text)
    names = names or None
    try:
        composed = await sched_module.compose_push(
            conn, chat_id, llm_chat=_push_llm_chat,
            names=names, mode=mode, rng=random.Random(),
        )
    except Exception as e:  # noqa: BLE001 — never let a push crash the scheduler
        log.error("compose_push failed for chat %s: %s", chat_id, e)
        return
    if composed is None:
        if names:
            log.info(
                "No push for chat %s (focus %r matched zero words)",
                chat_id, focus_text,
            )
        else:
            log.info("No push for chat %s (no vocab or chat missing)", chat_id)
        return
    word_id, word, text, is_intro = composed

    # Insert push_log first so the callback_data can reference a real push id;
    # update tg_message_id after the Telegram send returns.
    push_id = sched_module.log_push(conn, chat_id, tg_message_id=None, word_ids=[word_id])
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ knew", callback_data=f"rate:good:{push_id}:{word_id}"
            ),
            InlineKeyboardButton(
                "❌ forgot", callback_data=f"rate:again:{push_id}:{word_id}"
            ),
        ]]
    )
    formatted = vocab.format_push_body(word, text, intro=is_intro)
    try:
        msg = await app.bot.send_message(
            chat_id=chat_id, text=formatted, reply_markup=kb, parse_mode="HTML"
        )
        conn.execute(
            "UPDATE push_log SET tg_message_id = ? WHERE id = ?",
            (msg.message_id, push_id),
        )
        append_turn(chat_id, "push", f"[{word}] {text}")
    except Exception as e:  # noqa: BLE001
        log.error("Failed to send push to chat %s: %s", chat_id, e)


# --- command handlers -------------------------------------------------------


# Single source of truth for commands — used by /help and set_my_commands.
COMMANDS: list[tuple[str, str]] = [
    ("start", "Configure schedule: timezone, pushes/day, active window, tone, target language"),
    ("help", "Show this help message"),
    ("add", "Add a word or phrase to this chat's vocab"),
    ("remove", "Remove a word or phrase from vocab"),
    ("list", "List vocab words (every row shows its labels; optional label spec filter, AND across tokens; prepend --any for OR)"),
    ("import", "Bulk-import vocab from a CSV file (one word per row)"),
    ("export", "Download this chat's vocab as a CSV file"),
    ("resetvocab", "Wipe this chat's vocabulary (with confirm)"),
    ("tr", "Translate args; tap the button under the reply to add the English word/phrase to vocab"),
    ("games", "Pick a game: Word->Translation / Translation->Word (vocab quiz, optional label filter), Irregular verbs, Repeat typed (remembered words), or Focus drill typed (in-progress /focus words). /games cancel ends an in-flight game."),
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
    "1. Run /start and answer five questions: timezone, pushes per day (6–12), "
    "active window (HH:MM–HH:MM), tone, target language for /tr.\n"
    "2. Add words with /add <word or phrase> — or bulk-load a CSV with "
    "/import (and grab a backup any time with /export). The bot sends short "
    "snippets using those words at random times inside your window.\n"
    "3. Tap ✅ knew / ❌ forgot on each push — ratings drive FSRS spaced "
    "repetition so tougher words come back more often.\n"
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
    try:
        added = vocab.add_word(conn, chat_id, text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    normalized = text.strip().lower()
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
        had_game = (
            chat_id in games
            or chat_id in irregulars
            or chat_id in repeat_games
            or chat_id in focus_drills
        )
        games.pop(chat_id, None)
        irregulars.pop(chat_id, None)
        repeat_games.pop(chat_id, None)
        focus_drills.pop(chat_id, None)
        pending_game_filters.pop(chat_id, None)
        await update.message.reply_text(
            GAMES_CANCELLED if had_game else GAMES_NOTHING_TO_CANCEL
        )
        return
    if chat_id in games:
        await update.message.reply_text(GAMES_IN_PROGRESS)
        return
    if chat_id in irregulars:
        await update.message.reply_text(IRREGULARS_IN_PROGRESS)
        return
    if chat_id in repeat_games:
        await update.message.reply_text(REPEAT_IN_PROGRESS)
        return
    if chat_id in focus_drills:
        await update.message.reply_text(FOCUS_DRILL_IN_PROGRESS)
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
            [InlineKeyboardButton("Irregular verbs", callback_data="gm:irr")],
            [
                InlineKeyboardButton("Repeat (typed)", callback_data="gm:repeat"),
                InlineKeyboardButton("Focus drill (typed)", callback_data="gm:focus"),
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


def _format_repeat_prompt(game: repeat_module.Game) -> str:
    rd = game.current()
    return (
        f"Round {game.current_round + 1}/{game.n_rounds}: {rd.prompt}\n"
        f"{REPEAT_PROMPT_HINT}"
    )


async def _send_repeat_round(bot, chat_id: int, game: repeat_module.Game) -> None:
    await bot.send_message(chat_id=chat_id, text=_format_repeat_prompt(game))


def _format_focus_drill_prompt(game: focus_drill_module.Game) -> str:
    rd = game.current()
    return (
        f"Round {game.current_round + 1}/{game.n_rounds}: {rd.prompt}\n"
        f"{FOCUS_DRILL_PROMPT_HINT}"
    )


async def _send_focus_drill_round(
    bot, chat_id: int, game: focus_drill_module.Game
) -> None:
    await bot.send_message(chat_id=chat_id, text=_format_focus_drill_prompt(game))


# --- callback handlers ------------------------------------------------------


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    query = update.callback_query
    await query.answer()
    try:
        _, verdict, push_id_s, word_id_s = query.data.split(":")
        push_id, word_id = int(push_id_s), int(word_id_s)
    except (ValueError, AttributeError):
        log.warning("Malformed rate callback: %r", query.data)
        return
    rating = Rating.Good if verdict == "good" else Rating.Again
    try:
        vocab.rate_word(conn, word_id, rating)
        vocab.record_outcome(
            conn, word_id, correct=(rating == Rating.Good), weight=1.0, source="push"
        )
    except KeyError:
        log.warning("rate_word: unknown word_id %s", word_id)
    sched_module.mark_rated(conn, push_id)
    label = "✅ rated: knew" if rating == Rating.Good else "❌ rated: forgot"
    try:
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data="noop")]]
            )
        )
    except BadRequest:
        pass  # message too old or already replaced

    # On ❌ forgot, follow up with a tiny definition + example so the user can
    # learn the word on the spot.
    if rating != Rating.Again:
        return
    try:
        explanation = await sched_module.compose_explanation(
            conn, word_id, llm_chat=_push_llm_chat
        )
    except Exception as e:  # noqa: BLE001 — a failed explain shouldn't break rating
        log.error("explain failed for word %s: %s", word_id, e)
        return
    if not explanation:
        return
    chat_id = update.effective_chat.id
    # Reuse the rated word's text for the transcript tag and to mark it inline
    # under the 📌-header that matches the push format. Empty target_word when
    # the row vanished between rating and explanation suppresses the header.
    row = conn.execute(
        "SELECT text FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    tag_word = row["text"] if row is not None else "?"
    target_word = row["text"] if row is not None else ""
    formatted = vocab.format_push_body(target_word, explanation)

    # Tack on a single-word translation into the chat's configured target
    # language. A failure here mustn't drop the explanation itself.
    translation: str | None = None
    settings = config_flow.load_settings(conn, chat_id)
    if settings is not None:
        try:
            translation = await sched_module.compose_translation(
                conn,
                word_id,
                settings.target_lang,
                translate_fn=lambda w, t: asyncio.to_thread(
                    translator.translate, w, t, "en"
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.error("translate for explain failed for word %s: %s", word_id, e)

    body = sched_module.format_explanation_reply(formatted, translation)
    transcript = explanation if not translation else f"{explanation}\n→ {translation}"
    play_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎮 Play game", callback_data="pg:start")]]
    )
    try:
        await query.message.reply_text(
            body, parse_mode="HTML", reply_markup=play_kb, do_quote=True
        )
        append_turn(chat_id, "explain", f"[{tag_word}] {transcript}")
    except Exception as e:  # noqa: BLE001
        log.error("failed to send explanation for chat %s: %s", chat_id, e)


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
    if query.data == "gm:irr":
        if chat_id in games:
            await query.message.reply_text(GAMES_IN_PROGRESS)
            return
        if chat_id in irregulars:
            await query.message.reply_text(IRREGULARS_IN_PROGRESS)
            return
        if chat_id in repeat_games:
            await query.message.reply_text(REPEAT_IN_PROGRESS)
            return
        if chat_id in focus_drills:
            await query.message.reply_text(FOCUS_DRILL_IN_PROGRESS)
            return
        rounds = irregular_module.draw_rounds(rng=random.Random())
        game = irregular_module.Game(chat_id=chat_id, rounds=rounds)
        irregulars[chat_id] = game
        await query.message.reply_text(_format_irregular_prompt(game))
        return
    if query.data == "gm:repeat":
        if chat_id in games:
            await query.message.reply_text(GAMES_IN_PROGRESS)
            return
        if chat_id in irregulars:
            await query.message.reply_text(IRREGULARS_IN_PROGRESS)
            return
        if chat_id in repeat_games:
            await query.message.reply_text(REPEAT_IN_PROGRESS)
            return
        if chat_id in focus_drills:
            await query.message.reply_text(FOCUS_DRILL_IN_PROGRESS)
            return
        remembered_ids = vocab.remembered_word_ids(conn, chat_id)
        mastered_ids = vocab.mastered_word_ids(conn, chat_id)
        all_rows = vocab.list_words(conn, chat_id)
        pool = [
            r for r in all_rows
            if r["id"] in remembered_ids and r["id"] not in mastered_ids
        ]
        try:
            rounds = repeat_module.draw_rounds(pool, rng=random.Random())
        except ValueError:
            await query.message.reply_text(REPEAT_NOT_ENOUGH)
            return
        game = repeat_module.Game(chat_id=chat_id, rounds=rounds)
        repeat_games[chat_id] = game
        await _send_repeat_round(context.bot, chat_id, game)
        return
    if query.data == "gm:focus":
        if chat_id in games:
            await query.message.reply_text(GAMES_IN_PROGRESS)
            return
        if chat_id in irregulars:
            await query.message.reply_text(IRREGULARS_IN_PROGRESS)
            return
        if chat_id in repeat_games:
            await query.message.reply_text(REPEAT_IN_PROGRESS)
            return
        if chat_id in focus_drills:
            await query.message.reply_text(FOCUS_DRILL_IN_PROGRESS)
            return
        focus_text = vocab.get_focus_spec(conn, chat_id)
        mode, names_list = vocab.split_focus_spec(focus_text)
        names = names_list or None
        pool = _playable_rows(conn, chat_id, names, mode=mode)
        try:
            rounds = focus_drill_module.draw_rounds(pool, rng=random.Random())
        except ValueError:
            await query.message.reply_text(FOCUS_DRILL_NOT_ENOUGH)
            return
        game = focus_drill_module.Game(chat_id=chat_id, rounds=rounds)
        focus_drills[chat_id] = game
        await _send_focus_drill_round(context.bot, chat_id, game)
        return
    if query.data == "gm:wt":
        direction = "wt"
    elif query.data == "gm:tw":
        direction = "tw"
    else:
        log.warning("Malformed gm callback: %r", query.data)
        return
    if chat_id in games:
        await query.message.reply_text(GAMES_IN_PROGRESS)
        return
    if chat_id in irregulars:
        await query.message.reply_text(IRREGULARS_IN_PROGRESS)
        return
    if chat_id in repeat_games:
        await query.message.reply_text(REPEAT_IN_PROGRESS)
        return
    if chat_id in focus_drills:
        await query.message.reply_text(FOCUS_DRILL_IN_PROGRESS)
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


async def on_play_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the games direction-picker from the ❌-forgot explanation reply.

    Mirrors `cmd_games`'s gates (in-progress, MIN_VOCAB) and posts the same
    `gm:wt` / `gm:tw` menu. When the chat has a sticky `/focus` set, its
    names are seeded into `pending_game_filters` so the round draw uses the
    same restricted pool that pushes use.
    """
    if not is_allowed(update):
        return
    assert conn is not None
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    if chat_id in games:
        await query.message.reply_text(GAMES_IN_PROGRESS)
        return
    focus_text = vocab.get_focus_spec(conn, chat_id)
    mode, names_list = vocab.split_focus_spec(focus_text)
    names = names_list or None
    if len(_playable_rows(conn, chat_id, names, mode=mode)) < games_module.MIN_VOCAB:
        await query.message.reply_text(
            GAMES_NO_LABEL_MATCH if names else GAMES_NEED_VOCAB
        )
        return
    if names:
        pending_game_filters[chat_id] = (mode, names)
    else:
        pending_game_filters.pop(chat_id, None)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Word → Translation", callback_data="gm:wt"),
        InlineKeyboardButton("Translation → Word", callback_data="gm:tw"),
    ]])
    await query.message.reply_text("Pick a game:", reply_markup=kb)


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
        else:
            await update.message.reply_text(_format_irregular_prompt(irr_game))
        return

    # Branch 2b: repeat (typed) game in progress — interpret plain text as the
    # round answer, grade case-insensitively, record outcome.
    rep_game = repeat_games.get(chat_id)
    if rep_game is not None and not rep_game.done:
        rd = rep_game.current()
        correct = await repeat_module.grade_answer_llm(user_text, rd)
        if correct:
            reply = f"✅ {rd.expected}"
        else:
            reply = f"❌ correct: {rd.expected}"
        source_word = rd.prompt if rd.direction == "en2ru" else rd.expected
        repeat_module.apply_answer(rep_game, correct, source_word=source_word)
        try:
            # source="repeat" gates the forget-flip in record_outcome — a wrong
            # answer here drops `remembered` and attaches `focus:hard`.
            vocab.record_outcome(
                conn, rd.word_id, correct=correct, weight=0.5, source="repeat"
            )
        except KeyError:
            log.warning("record_outcome: unknown word_id %s", rd.word_id)
        await update.message.reply_text(reply)
        if rep_game.done:
            await update.message.reply_text(
                repeat_module.format_result(
                    rep_game.score, rep_game.n_rounds, rep_game.wrong
                )
            )
            repeat_games.pop(chat_id, None)
        else:
            await update.message.reply_text(_format_repeat_prompt(rep_game))
        return

    # Branch 2c: focus drill (typed) — same shape as repeat, but words are
    # non-remembered focus-scoped rows, so we use source="game" (no forget-flip).
    fd_game = focus_drills.get(chat_id)
    if fd_game is not None and not fd_game.done:
        rd = fd_game.current()
        correct = focus_drill_module.grade_answer(user_text, rd)
        if correct:
            reply = f"✅ {rd.expected}"
        else:
            reply = f"❌ correct: {rd.expected}"
        source_word = rd.prompt if rd.direction == "en2ru" else rd.expected
        focus_drill_module.apply_answer(
            fd_game, correct, source_word=source_word
        )
        try:
            vocab.record_outcome(
                conn, rd.word_id, correct=correct, weight=0.5, source="game"
            )
        except KeyError:
            log.warning("record_outcome: unknown word_id %s", rd.word_id)
        await update.message.reply_text(reply)
        if fd_game.done:
            await update.message.reply_text(
                focus_drill_module.format_result(
                    fd_game.score, fd_game.n_rounds, fd_game.wrong
                )
            )
            focus_drills.pop(chat_id, None)
        else:
            await update.message.reply_text(_format_focus_drill_prompt(fd_game))
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
    append_turn(chat_id, "user", user_text)

    current_msg = await update.message.reply_text(CURSOR)
    current_page = ""
    accumulated = ""
    last_edit = asyncio.get_event_loop().time()

    def fmt(s: str) -> str:
        return vocab.highlight_matches(s, word_texts)

    try:
        async for token in llm.stream_chat(histories[chat_id]):
            accumulated += token
            current_page += token

            while len(current_page) > MAX_MSG_LEN:
                idx = split_point(current_page, MAX_MSG_LEN)
                head, current_page = current_page[:idx], current_page[idx:]
                await safe_edit(current_msg, fmt(head), parse_mode="HTML")
                current_msg = await safe_send(
                    context.bot, chat_id, fmt(current_page + CURSOR), parse_mode="HTML"
                )
                last_edit = asyncio.get_event_loop().time()

            now = asyncio.get_event_loop().time()
            if now - last_edit >= EDIT_INTERVAL:
                await safe_edit(current_msg, fmt(current_page + CURSOR), parse_mode="HTML")
                last_edit = now

    except Exception as e:
        log.error("Streaming error: %s", e)
        append_turn(chat_id, "error", f"{type(e).__name__}: {e}")
        await safe_edit(current_msg, fmt(current_page or f"⚠️ Error: {e}"), parse_mode="HTML")
        return

    final_text = current_page or "⚠️ No response from model."
    await safe_edit(current_msg, fmt(final_text), parse_mode="HTML")

    histories[chat_id].append({"role": "assistant", "content": accumulated})
    append_turn(chat_id, "assistant", accumulated)

    # Count any vocab words literally present in the reply.
    id_pairs = [(r["id"], r["text"]) for r in vocab_rows]
    mentioned = vocab.scan_mentions(accumulated, id_pairs)
    if mentioned:
        vocab.bump_mentions(conn, mentioned)


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
    app.add_handler(CallbackQueryHandler(on_play_game, pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(on_game_answer, pattern=r"^g:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
