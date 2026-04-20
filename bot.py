#!/usr/bin/env python3
"""Gemma 4 Telegram bot with live-streaming message edits."""

import asyncio
import json
import logging
import os

import httpx
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

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant running on a Raspberry Pi.")
LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "gemma4"
CURSOR = "▌"
EDIT_INTERVAL = 2.0   # seconds between Telegram message edits (rate-limit safe)
MAX_MSG_LEN = 4000    # Telegram hard limit is 4096

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# Per-chat conversation history stored in memory
histories: dict[int, list[dict]] = {}


def fresh_history() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def sys_footer() -> str:
    load1, load5, load15 = os.getloadavg()
    try:
        temp_mc = int(open("/sys/class/thermal/thermal_zone0/temp").read())
        temp_str = f"{temp_mc / 1000:.1f}°C"
    except OSError:
        temp_str = "n/a"
    return f"\n\n`load {load1:.2f} {load5:.2f} {load15:.2f} | {temp_str}`"


async def stream_llama(messages: list[dict]):
    """Async-stream token chunks from the llama.cpp OpenAI-compatible endpoint."""
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            LLAMA_URL,
            json={
                "model": MODEL,
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0.7,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError):
                    continue


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    if not user_text:
        return

    if chat_id not in histories:
        histories[chat_id] = fresh_history()

    histories[chat_id].append({"role": "user", "content": user_text})

    # Send placeholder so we have a message to edit
    sent = await update.message.reply_text(CURSOR)

    accumulated = ""
    last_edit = asyncio.get_event_loop().time()

    try:
        async for token in stream_llama(histories[chat_id]):
            accumulated += token

            # Truncate if approaching Telegram's limit
            display = accumulated[-MAX_MSG_LEN:] if len(accumulated) > MAX_MSG_LEN else accumulated

            now = asyncio.get_event_loop().time()
            if now - last_edit >= EDIT_INTERVAL:
                await safe_edit(sent, display + CURSOR)
                last_edit = now

    except Exception as e:
        log.error("Streaming error: %s", e)
        await safe_edit(sent, accumulated or f"⚠️ Error: {e}")
        return

    # Final message without cursor, with system stats appended
    footer = sys_footer()
    display = accumulated[-MAX_MSG_LEN:] if len(accumulated) > MAX_MSG_LEN else accumulated
    await safe_edit(sent, (display or "⚠️ No response from model.") + footer)

    histories[chat_id].append({"role": "assistant", "content": accumulated})


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    histories[chat_id] = fresh_history()
    await update.message.reply_text(
        "👋 Gemma 4 bot is ready.\n\n"
        "Just send a message to chat. Responses stream live.\n\n"
        "/clear — reset conversation\n"
        "/model — show model info"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    histories[update.effective_chat.id] = fresh_history()
    await update.message.reply_text("Conversation cleared.")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("http://127.0.0.1:8080/health")
            status = r.json().get("status", "unknown")
    except Exception as e:
        status = f"unreachable ({e})"

    await update.message.reply_text(
        f"Model: {MODEL}\n"
        f"Endpoint: {LLAMA_URL}\n"
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
