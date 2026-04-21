#!/usr/bin/env python3
"""Gemma 4 Telegram bot with live-streaming message edits."""

import asyncio
import datetime
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import RetryAfter, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

import llm

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant running on a Raspberry Pi.")
# Comma- or whitespace-separated Telegram user IDs allowed to talk to the bot.
# Empty/unset means no restriction (all users allowed).
ALLOWED_USER_IDS: set[int] = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(",", " ").split() if x.strip()
}
CURSOR = "▌"
EDIT_INTERVAL = 2.0   # seconds between Telegram message edits (rate-limit safe)
MAX_MSG_LEN = 4000    # Telegram hard limit is 4096; responses past this spill into new messages

LOGS_DIR = Path(__file__).resolve().parent / "logs"
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
log = logging.getLogger(__name__)

# Per-chat conversation history stored in memory
histories: dict[int, list[dict]] = {}
# Per-chat path of the active transcript file (rotated on /start and /clear).
conv_paths: dict[int, Path] = {}


def fresh_history() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


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
    """Return an index to split `text` so the head fits within `max_len`.

    Prefers paragraph > line > sentence > word boundaries in the upper half of
    the window, falling back to a hard cut at `max_len` if none are found.
    """
    if len(text) <= max_len:
        return len(text)
    floor = max_len // 2
    for sep, offset in (("\n\n", 2), ("\n", 1), (". ", 2), ("! ", 2), ("? ", 2), (" ", 1)):
        idx = text.rfind(sep, floor, max_len)
        if idx != -1:
            return idx + offset
    return max_len


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    if not user_text:
        return

    if chat_id not in histories:
        histories[chat_id] = fresh_history()
        start_conversation(chat_id)

    histories[chat_id].append({"role": "user", "content": user_text})
    append_turn(chat_id, "user", user_text)

    # First message is a reply to the user; continuation messages go to the chat directly.
    current_msg = await update.message.reply_text(CURSOR)
    current_page = ""      # text rendered in current_msg
    accumulated = ""       # full response (for history)
    last_edit = asyncio.get_event_loop().time()

    try:
        async for token in llm.stream_chat(histories[chat_id]):
            accumulated += token
            current_page += token

            while len(current_page) > MAX_MSG_LEN:
                idx = split_point(current_page, MAX_MSG_LEN)
                head, current_page = current_page[:idx], current_page[idx:]
                await safe_edit(current_msg, head)
                current_msg = await safe_send(context.bot, chat_id, current_page + CURSOR)
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

    # Final page without cursor, with system stats appended (spill footer if needed)
    footer = sys_footer()
    final_text = current_page or "⚠️ No response from model."
    if len(final_text) + len(footer) > MAX_MSG_LEN:
        await safe_edit(current_msg, final_text)
        await safe_send(context.bot, chat_id, footer.lstrip())
    else:
        await safe_edit(current_msg, final_text + footer)

    histories[chat_id].append({"role": "assistant", "content": accumulated})
    append_turn(chat_id, "assistant", accumulated)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    histories[chat_id] = fresh_history()
    start_conversation(chat_id)
    await update.message.reply_text(
        "👋 Gemma 4 bot is ready.\n\n"
        "Just send a message to chat. Responses stream live.\n\n"
        "/clear — reset conversation\n"
        "/model — show model info"
    )


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


def main() -> None:
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30)
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
