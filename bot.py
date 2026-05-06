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
import games as games_module
import llm
import prompts
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
# Tokens → (chat_id, word) for pending /translate "Add to vocab" buttons.
pending_vocab: translator.PendingVocab = translator.PendingVocab()
# Chats that recently issued /import: chat_id → expiry monotonic timestamp.
# The next document upload from these chats is parsed as a vocab CSV.
import_pending: dict[int, float] = {}
# In-flight /games sessions: at most one per chat (AC8). Cleared on completion
# and on bot restart — game state is intentionally not persisted.
games: dict[int, games_module.Game] = {}

GAMES_NEED_VOCAB = "add at least 4 words to your vocab first"
GAMES_IN_PROGRESS = "you have a game in progress"
GAMES_TW_COMING_SOON = "coming soon"


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


# --- push dispatch (called by the scheduler) --------------------------------


async def dispatch_push(chat_id: int) -> None:
    """Compose a scheduled push and send it with rating buttons."""
    assert conn is not None and app is not None
    try:
        composed = await sched_module.compose_push(
            conn, chat_id, llm_chat=llm.chat, rng=random.Random()
        )
    except Exception as e:  # noqa: BLE001 — never let a push crash the scheduler
        log.error("compose_push failed for chat %s: %s", chat_id, e)
        return
    if composed is None:
        log.info("No push for chat %s (no vocab or chat missing)", chat_id)
        return
    word_id, word, text = composed

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
    chat_words = [r["text"] for r in vocab.list_words(conn, chat_id)]
    formatted = vocab.highlight_matches(text, chat_words)
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
    ("list", "List vocab words (optionally filter by substring)"),
    ("import", "Bulk-import vocab from a CSV file (one word per row)"),
    ("export", "Download this chat's vocab as a CSV file"),
    ("resetvocab", "Wipe this chat's vocabulary (with confirm)"),
    ("translate", "Translate args; tap the button under the reply to add the English word/phrase to vocab"),
    ("games", "Play a vocab quiz (Word → Translation, 1–10 rounds)"),
    ("clear", "Reset the chat history (LLM memory)"),
    ("status", "Show host diagnostics, vocab count, and a short model bench"),
]

HELP_TEXT = (
    "🤖 *Gemma vocab agent*\n\n"
    "*Getting started*\n"
    "1. Run /start and answer five questions: timezone, pushes per day (6–12), "
    "active window (HH:MM–HH:MM), tone, target language for /translate.\n"
    "2. Add words with /add <word or phrase> — or bulk-load a CSV with "
    "/import (and grab a backup any time with /export). The bot sends short "
    "snippets using those words at random times inside your window.\n"
    "3. Tap ✅ knew / ❌ forgot on each push — ratings drive FSRS spaced "
    "repetition so tougher words come back more often.\n\n"
    "Plain (non-slash) messages chat with the model and will naturally reuse "
    "your vocab when it fits.\n\n"
    "*Commands*\n"
    + "\n".join(f"/{name} — {desc}" for name, desc in COMMANDS)
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
    words = vocab.count_words(conn, chat_id)
    server = await llm.health()
    bench_line = await llm.bench()

    await update.message.reply_text(
        "System\n"
        f"  Hardware: {hardware}\n"
        f"  OS: {os_name}\n"
        f"  Load: {load1:.2f} {load5:.2f} {load15:.2f}\n"
        f"  Temp: {temp}\n"
        f"  Disk /: {sysinfo.format_bytes(free)} free / "
        f"{sysinfo.format_bytes(total)}\n"
        f"  Vocab: {words} words\n"
        "\n"
        "Model\n"
        f"  Name: {llm.MODEL}\n"
        f"  Endpoint: {llm.LLAMA_URL}\n"
        f"  Server: {server}\n"
        f"  Bench: {bench_line}"
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
    needle = " ".join(context.args or []).strip() or None
    rows = vocab.list_words(conn, chat_id, contains=needle)
    if not rows:
        msg = (
            f"No words matching '{needle}'."
            if needle
            else "Your vocab is empty. Add words with /add <word>."
        )
        await update.message.reply_text(msg)
        return
    header = (
        f"Vocab ({len(rows)})"
        + (f" matching '{needle}'" if needle else "")
        + ":"
    )
    scores = vocab.compute_scores(rows)
    lines = [
        f"• {r['text']} (seen {r['mention_count']}×, score {s})"
        for r, s in zip(rows, scores)
    ]
    # Telegram message cap — truncate gracefully.
    body = "\n".join(lines)
    if len(body) > MAX_MSG_LEN - len(header) - 32:
        body = body[: MAX_MSG_LEN - len(header) - 32] + "\n…"
    await update.message.reply_text(f"{header}\n{body}")


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
    csv_text = vocab.format_csv([(r["text"], r["translation"]) for r in rows])
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
        pairs = vocab.parse_csv_words(text)
    except csv.Error as e:
        await update.message.reply_text(f"⚠️ Could not parse CSV: {e}")
        return
    if not pairs:
        await update.message.reply_text("No words found in the file.")
        return
    if len(pairs) > IMPORT_MAX_ROWS:
        await update.message.reply_text(
            f"⚠️ Too many rows ({len(pairs)}; max {IMPORT_MAX_ROWS})."
        )
        return
    words = [w for w, _ in pairs]
    translations = [t for _, t in pairs]
    counts = vocab.add_words_bulk(conn, chat_id, words, translations=translations)
    parts = [
        f"added: {counts['added']}",
        f"skipped (duplicate): {counts['skipped']}",
    ]
    if counts["invalid"]:
        parts.append(f"skipped (empty): {counts['invalid']}")
    await update.message.reply_text("Imported. " + ", ".join(parts) + ".")


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
            "Usage: /translate <text>, or reply to a message with /translate.",
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


def _playable_rows(conn_: sqlite3.Connection, chat_id: int) -> list[sqlite3.Row]:
    return [r for r in vocab.list_words(conn_, chat_id) if r["translation"]]


def _round_keyboard(game: games_module.Game) -> InlineKeyboardMarkup:
    rd = game.current()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(opt, callback_data=f"g:{game.current_round}:{i}")]
        for i, opt in enumerate(rd.options)
    ])


async def _send_round(bot, chat_id: int, game: games_module.Game) -> None:
    rd = game.current()
    text = f"Round {game.current_round + 1}/{game.n_rounds}: {rd.text}"
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=_round_keyboard(game))


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    if chat_id in games:
        await update.message.reply_text(GAMES_IN_PROGRESS)
        return
    if len(_playable_rows(conn, chat_id)) < games_module.MIN_VOCAB:
        await update.message.reply_text(GAMES_NEED_VOCAB)
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Word → Translation", callback_data="gm:wt"),
        InlineKeyboardButton("Translation → Word", callback_data="gm:tw"),
    ]])
    await update.message.reply_text("Pick a game:", reply_markup=kb)


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
            conn, word_id, llm_chat=llm.chat
        )
    except Exception as e:  # noqa: BLE001 — a failed explain shouldn't break rating
        log.error("explain failed for word %s: %s", word_id, e)
        return
    if not explanation:
        return
    chat_id = update.effective_chat.id
    # Reuse the rated word's text for the transcript tag and to highlight it inline.
    row = conn.execute(
        "SELECT text FROM words WHERE id = ?", (word_id,)
    ).fetchone()
    tag_word = row["text"] if row is not None else "?"
    formatted = vocab.highlight_matches(
        explanation, [tag_word] if row is not None else []
    )

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
    try:
        await query.message.reply_text(body, parse_mode="HTML", do_quote=True)
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
    if query.data == "gm:tw":
        await query.message.reply_text(GAMES_TW_COMING_SOON)
        return
    # gm:wt — start Word → Translation.
    if chat_id in games:
        await query.message.reply_text(GAMES_IN_PROGRESS)
        return
    rows = _playable_rows(conn, chat_id)
    if len(rows) < games_module.MIN_VOCAB:
        await query.message.reply_text(GAMES_NEED_VOCAB)
        return
    rounds = games_module.draw_rounds(rows, rng=random.Random())
    game = games_module.Game(chat_id=chat_id, rounds=rounds)
    games[chat_id] = game
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

    # Branch 2: just-talk. Inject vocab words into the system prompt so the
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
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(CommandHandler("games", cmd_games))

    app.add_handler(CallbackQueryHandler(on_rate, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(on_resetvocab_confirm, pattern=r"^rv:"))
    app.add_handler(CallbackQueryHandler(on_add_vocab, pattern=r"^av:"))
    app.add_handler(CallbackQueryHandler(on_games_menu, pattern=r"^gm:"))
    app.add_handler(CallbackQueryHandler(on_game_answer, pattern=r"^g:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))

    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
