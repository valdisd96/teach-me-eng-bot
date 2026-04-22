# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Telegram bot that streams responses from a local **Gemma 4** model running via **llama.cpp**'s OpenAI-compatible HTTP server on `http://127.0.0.1:8080`, plus an **FSRS-driven English-vocabulary agent**: the user adds words with `/add`, and the bot sends scheduled push messages that use those words in short tone-flavoured snippets. Rating buttons (✅ knew / ❌ forgot) update FSRS state per word. Plain chat messages still stream live via placeholder message edits.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# For running tests: pip install -r requirements-dev.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN
```

## Running

```bash
source .venv/bin/activate
python bot.py
```

```bash
source .venv/bin/activate && python -m pytest -q    # run tests
```

The llama.cpp server must already be running on `http://127.0.0.1:8080` before starting the bot.

## Environment variables (`.env`)

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | `"You are a friendly English tutor chatting casually with a learner. Use natural, everyday English. If they ask about grammar, vocabulary, or usage, explain briefly with a small example."` |
| `ALLOWED_USER_IDS` | No | empty (allow all) — comma/whitespace-separated Telegram user IDs; if set, other users are silently ignored and logged |

Per-chat scheduling settings (timezone, pushes-per-day, active window, tone) are collected via the `/start` conversation flow and stored in SQLite — they are **not** environment variables.

## Bot commands

| Command | Purpose |
|---|---|
| `/start` | Walks a guided config: timezone → pushes/day (1–8) → active window (HH:MM) → tone (funny/motivational/scary/bright/mixed). Overwrites previous settings; vocab is preserved. |
| `/help` | Shows a getting-started intro and lists all commands with descriptions. |
| `/clear` | Resets the chat history (LLM memory) for this chat. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. Normalized to lowercased+stripped form. |
| `/remove <word or phrase>` | Remove by exact (normalized) match. |
| `/list [substring]` | List all vocab words (least-mentioned first), or only those matching a substring. |
| `/resetvocab` | Wipes the chat's vocabulary (with a confirm button). |
| `/model` | Shows the llama.cpp endpoint and health status. |

Plain (non-slash) messages go through the **just-talk** flow: the chat history is passed to the model with the current vocab list injected into the system prompt as soft hints. Any vocab words that appear literally in the reply bump `mention_count` and update `last_used_at`.

Scheduled **pushes** send 1 short snippet using 1 vocab word at a time, in the chosen tone. Each push message has `✅ knew / ❌ forgot` buttons; tapping either applies the corresponding FSRS rating (`Good` / `Again`).

## Architecture

Code is split into focused modules (entrypoint is `bot.py`):

- **`bot.py`** — python-telegram-bot wiring. Command/callback/message handlers, scheduler bootstrap, transcript/history management, DB connection lifecycle.
- **`llm.py`** — llama.cpp HTTP client. `stream_chat()` for live edits, `chat()` one-shot for pushes, `health()` for `/model`. SSE/completion parsing is factored into pure helpers.
- **`vocab.py`** — vocabulary CRUD, literal mention scanning, FSRS rating (`rate_word`), and the weighted-random `select_word`. Uses `py-fsrs` with `desired_retention=0.95` and `maximum_interval=7d` so review intervals stay tight.
- **`prompts.py`** — tone-flavoured push templates and the just-talk system-prompt composer that appends the chat's vocab as soft hints.
- **`config_flow.py`** — `Settings` dataclass + `ConfigSession` state machine for `/start`; per-step validators (IANA tz, 1–8 pushes, HH:MM, known tone); `save_settings` / `load_settings` upsert against the `chats` table.
- **`scheduler.py`** — `plan_push_times` (equal-bucket sampling with half-gap edge buffers), `compose_push` (select + LLM call + retry once if the word didn't appear literally), `log_push` / `mark_rated`, and `PushRunner` wrapping `AsyncIOScheduler` with per-chat daily re-planning at 00:01 local.
- **`db.py`** — SQLite schema (`chats`, `words`, `push_log`) with FSRS columns on `words`; `connect()` sets WAL + `PRAGMA foreign_keys=ON`; `init_db()` applies forward-only column migrations.
- **`tests/`** — pytest suite covering schema constraints, vocab CRUD, mention scanning, FSRS state transitions, selection-weight math, deterministic weighted sampling, prompt composition, tz/time validators, config session transitions, plan_push_times determinism + min-gap, compose_push retry paths, push_log roundtrip, PushRunner job registration.

### Selection weight

`vocab.select_word` samples via `random.choices` using:

```
weight(row) = (1 + forget_prob)     # FSRS; 1 - retrievability; unrated → 1.0
            * (1 + recency_boost)    # exp(-age_days / 7)
            * (1 + rarity_boost)     # 1 / (1 + mention_count)
```

Each factor lives in `[0, 1]`, lifted to `[1, 2]` so no single signal dominates.

## SQLite

- Path: `data/vocab.db` (git-ignored). Created on first run.
- Tables:
  - `chats(chat_id PK, tz, pushes_per_day, active_start, active_end, tone, created_at)`
  - `words(id PK, chat_id FK, text, added_at, mention_count, last_used_at, stability, difficulty, state, step, due, reps, lapses, last_review, UNIQUE(chat_id, text))`
  - `push_log(id PK, chat_id, sent_at, tg_message_id, word_ids_json, rated)`
- Cascade-delete: removing a chat drops its words and push_log entries.

## Logs on disk

- `logs/bot.log` — rotating file log (5 MB × 5 backups), mirrors journald output. `httpx` / `telegram` / `apscheduler` loggers are pinned to WARNING so the bot token never appears in request URLs.
- `logs/convs/<chat_id>/NNN.txt` — human-readable transcript, one file per conversation, zero-padded sequential numbering per chat. Push messages are included tagged `push`.
- `logs/` and `data/` are git-ignored.

## Key constants

- `bot.py`: `EDIT_INTERVAL = 2.0` (seconds between stream edits), `MAX_MSG_LEN = 4000` (per-message cap; long responses spill into additional messages).
- `llm.py`: `LLAMA_URL`, `MODEL = "gemma4"`.
- `vocab.py`: `FSRS_RETENTION = 0.95`, `FSRS_MAX_DAYS = 7`, `RECENCY_TAU_DAYS = 7.0`.
- `scheduler.py`: `MIN_GAP_MIN = 45` (minimum spacing between consecutive pushes, enforced implicitly by the bucket algorithm).
