# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file Telegram bot (`bot.py`) that streams responses from a local **Gemma 4** model running via **llama.cpp**'s OpenAI-compatible HTTP server on `http://127.0.0.1:8080`. Responses are streamed live by repeatedly editing the placeholder Telegram message.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN
```

## Running

```bash
source .venv/bin/activate
python bot.py
```

The llama.cpp server must already be running on `http://127.0.0.1:8080` before starting the bot.

## Environment variables (`.env`)

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | `"You are a helpful assistant running on a Raspberry Pi."` |
| `ALLOWED_USER_IDS` | No | empty (allow all) — comma/whitespace-separated Telegram user IDs; if set, other users are silently ignored and logged |

## Architecture

All logic lives in `bot.py`:

- **`stream_llama(messages)`** — async generator that POSTs to the llama.cpp `/v1/chat/completions` SSE endpoint and yields token strings.
- **`handle_message()`** — core handler: appends user turn to per-chat `histories[chat_id]`, sends a cursor placeholder, then consumes `stream_llama` tokens and edits the placeholder every `EDIT_INTERVAL` seconds (2 s, safe under Telegram rate limits). When the current message exceeds `MAX_MSG_LEN`, it is finalized at a clean break (paragraph/line/sentence/word via `split_point`) and streaming continues in a freshly sent message. Full accumulated text is stored back into history.
- **`histories`** — in-memory `dict[int, list[dict]]` keyed by `chat_id`; each value is a standard OpenAI-format message list starting with the system prompt. Cleared on `/start` or `/clear`.
- **`safe_edit()`** — wraps `message.edit_text` to absorb `RetryAfter` (sleeps then retries) and `BadRequest: message is not modified` (ignored).

## Key constants (top of `bot.py`)

```python
LLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "gemma4"
EDIT_INTERVAL = 2.0   # seconds between edits
MAX_MSG_LEN = 4000    # per-message cap; long responses spill into additional messages
```
