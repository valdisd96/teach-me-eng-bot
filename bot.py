#!/usr/bin/env python3
"""Gemma 4 Telegram bot — streaming chat + FSRS-driven vocab agent.

Split responsibilities:
  * llm.py           — llama.cpp client (stream + one-shot + health)
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
import datetime
import logging
import os
import random
import sqlite3
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
import llm
import prompts
import scheduler as sched_module
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


def sys_footer() -> str:
    load1, load5, load15 = os.getloadavg()
    try:
        temp_mc = int(open("/sys/class/thermal/thermal_zone0/temp").read())
        temp_str = f"{temp_mc / 1000:.1f}°C"
    except OSError:
        temp_str = "n/a"
    return f"\n\n`load {load1:.2f} {load5:.2f} {load15:.2f} | {temp_str}`"


# --- telegram send helpers --------------------------------------------------


async def safe_edit(message, text: str) -> None:
    """Edit a Telegram message, handling rate-limit and no-change errors."""
    try:
        await message.edit_text(text)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        await message.edit_text(text)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def safe_send(bot, chat_id: int, text: str):
    """Send a new Telegram message, retrying once on RetryAfter."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        return await bot.send_message(chat_id=chat_id, text=text)


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
    try:
        msg = await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
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
    ("resetvocab", "Wipe this chat's vocabulary (with confirm)"),
    ("translate", "Translate args, or reply to a message with /translate"),
    ("clear", "Reset the chat history (LLM memory)"),
    ("model", "Show the llama.cpp endpoint and health"),
]

HELP_TEXT = (
    "🤖 *Gemma vocab agent*\n\n"
    "*Getting started*\n"
    "1. Run /start and answer five questions: timezone, pushes per day (6–12), "
    "active window (HH:MM–HH:MM), tone, target language for /translate.\n"
    "2. Add words with /add <word or phrase>. The bot sends short snippets "
    "using those words at random times inside your window.\n"
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


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    status = await llm.health()
    await update.message.reply_text(
        f"Model: {llm.MODEL}\n"
        f"Endpoint: {llm.LLAMA_URL}\n"
        f"Server status: {status}"
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
    lines = [f"• {r['text']} (seen {r['mention_count']}×)" for r in rows]
    # Telegram message cap — truncate gracefully.
    body = "\n".join(lines)
    if len(body) > MAX_MSG_LEN - len(header) - 32:
        body = body[: MAX_MSG_LEN - len(header) - 32] + "\n…"
    await update.message.reply_text(f"{header}\n{body}")


async def cmd_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    assert conn is not None
    chat_id = update.effective_chat.id
    settings = config_flow.load_settings(conn, chat_id)
    if settings is None:
        await update.message.reply_text(
            "Run /start first so I know which language to translate into."
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
            "Usage: /translate <text>, or reply to a message with /translate."
        )
        return

    try:
        translated = await asyncio.to_thread(
            translator.translate, source_text, settings.target_lang
        )
    except Exception as e:  # noqa: BLE001 — surface the reason to the user
        log.error("translate failed for chat %s: %s", chat_id, e)
        await update.message.reply_text(f"⚠️ Translation failed: {e}")
        return
    await update.message.reply_text(translated or "⚠️ Empty translation.")


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
    try:
        await query.message.reply_text(explanation)
        # Best-effort: reuse the rated word's text for the transcript tag.
        row = conn.execute(
            "SELECT text FROM words WHERE id = ?", (word_id,)
        ).fetchone()
        tag_word = row["text"] if row is not None else "?"
        append_turn(chat_id, "explain", f"[{tag_word}] {explanation}")
    except Exception as e:  # noqa: BLE001
        log.error("failed to send explanation for chat %s: %s", chat_id, e)


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

    try:
        async for token in llm.stream_chat(histories[chat_id]):
            accumulated += token
            current_page += token

            while len(current_page) > MAX_MSG_LEN:
                idx = split_point(current_page, MAX_MSG_LEN)
                head, current_page = current_page[:idx], current_page[idx:]
                await safe_edit(current_msg, head)
                current_msg = await safe_send(
                    context.bot, chat_id, current_page + CURSOR
                )
                last_edit = asyncio.get_event_loop().time()

            now = asyncio.get_event_loop().time()
            if now - last_edit >= EDIT_INTERVAL:
                await safe_edit(current_msg, current_page + CURSOR)
                last_edit = now

    except Exception as e:
        log.error("Streaming error: %s", e)
        append_turn(chat_id, "error", f"{type(e).__name__}: {e}")
        await safe_edit(current_msg, current_page or f"⚠️ Error: {e}")
        return

    footer = sys_footer()
    final_text = current_page or "⚠️ No response from model."
    if len(final_text) + len(footer) > MAX_MSG_LEN:
        await safe_edit(current_msg, final_text)
        await safe_send(context.bot, chat_id, footer.lstrip())
    else:
        await safe_edit(current_msg, final_text + footer)

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
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("resetvocab", cmd_resetvocab))
    app.add_handler(CommandHandler("translate", cmd_translate))

    app.add_handler(CallbackQueryHandler(on_rate, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(on_resetvocab_confirm, pattern=r"^rv:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
