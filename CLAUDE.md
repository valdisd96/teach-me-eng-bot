# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Telegram English-tutor bot. Users add vocabulary with `/add`; the bot sends FSRS-scheduled push messages that use those words in short tone-flavoured snippets, with ✅ knew / ❌ forgot buttons that update each word's FSRS state. Plain chat messages stream live replies from any OpenAI-compatible chat-completions endpoint with the chat's vocab injected as soft hints.

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

The bot needs an OpenAI-compatible chat-completions endpoint reachable per `LLM_BACKEND`. Default backend is a local server on `http://127.0.0.1:8080`; production deployments use `LLM_BACKEND=openrouter`.

## Environment variables (`.env`)

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | `"You are a friendly English tutor chatting casually with a learner. Use natural, everyday English. If they ask about grammar, vocabulary, or usage, explain briefly with a small example."` |
| `ALLOWED_USER_IDS` | No | empty (allow all) — comma/whitespace-separated Telegram user IDs; if set, other users are silently ignored and logged |
| `LLM_BACKEND` | No | `llama` — local OpenAI-compatible server on `http://127.0.0.1:8080`. Set to `openrouter` to route chat completions to OpenRouter instead (the production backend). |
| `OPENROUTER_API_KEY` | When `LLM_BACKEND=openrouter` | — sent as `Authorization: Bearer <key>`. Empty value with `LLM_BACKEND=openrouter` raises at the first LLM call. |
| `OPENROUTER_MODEL` | No | A free-tier OpenRouter model id (see `.env.example` for the current default). Override to swap models without code changes. |

Per-chat scheduling settings (timezone, pushes-per-day, active window, tone) are collected via the `/start` conversation flow and stored in SQLite — they are **not** environment variables.

## Bot commands

| Command | Purpose |
|---|---|
| `/start` | Walks a guided config: timezone → pushes/day (6–12) → active window (HH:MM) → tone (funny/motivational/scary/bright/mixed) → target language (for `/translate`). Overwrites previous settings; vocab is preserved. |
| `/help` | Shows a getting-started intro and lists all commands with descriptions. |
| `/clear` | Resets the chat history (LLM memory) for this chat. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. Normalized to lowercased+stripped form. |
| `/remove <word or phrase>` | Remove by exact (normalized) match. |
| `/list [substring]` | List all vocab words (least-mentioned first), or only those matching a substring. |
| `/import` | Bulk-import vocab from a CSV file. Bot prompts for an upload (5-min window). One word/phrase per row in the first column; an optional `text` header is supported. Words are normalized + deduped against existing vocab; merge semantics — existing words are preserved, in-file duplicates are skipped. Reply summarizes `added` / `skipped (duplicate)` / `skipped (empty)`. Capped at 5000 rows / 1 MB. FSRS state is **not** imported. |
| `/export` | Send this chat's vocab back as a CSV attachment (`vocab-YYYY-MM-DD.csv`), one word per row, alphabetically sorted, with a `text` header. FSRS state is **not** exported. |
| `/resetvocab` | Wipes the chat's vocabulary (with a confirm button). |
| `/translate <text>` | Google-translate the args (or, if sent as a reply, the replied message) into the chat's configured target language. If the input is written in the target's script (e.g. Cyrillic for `ru`), reverse-translate it to English instead. The reply carries an `➕ Add to vocab` button that, when tapped, adds the English side of the pair (the source when forward-translating, the translation when reverse-translating) to this chat's vocab — the button label flips to `added to vocab ✅` or `already in vocab`. Phrases longer than 5 words skip the button and show the inline note `not added (N words)`. Does **not** invoke the LLM. Reverse detection only fires for non-Latin targets. |
| `/games` | Pick a vocab quiz from a 2-button menu: **Word → Translation** (prompt = English word, buttons = translations) or **Translation → Word** (prompt = translation, buttons = English words). Either game runs `min(10, vocab_size)` rounds with 4 inline buttons per round (1 correct + 3 distractors drawn from the same game's pool, randomly ordered); a tap edits the keyboard with ✅/❌ feedback and advances. After the last round the bot sends `🎯 You scored X/N`. Game state is in-memory only (a restart abandons in-flight games), and only one game per chat at a time — a second `/games` while a game is in progress replies `you have a game in progress`. With fewer than 4 vocab rows the menu is suppressed and the bot replies `add at least 4 words to your vocab first`. |
| `/label <word> <spec>...` | Attach one or more labels to a vocab word. Spec tokens are bare strings (`medicine`) or `key:value` (`pos:noun`, `type:animal`); each token is stripped + lowercased, duplicates are dropped, malformed tokens (`:`, `:foo`, `foo:`, `a:b:c`, internal whitespace) abort the call with a ⚠️ error and no writes. Word lookup is case-insensitive (matches `/remove`). Idempotent — re-applying an attached label replies `already attached: …` and inserts nothing. Attaching `pos:*` to a word that already has a different `pos:*` quietly replaces it; the reply names both, e.g. `horse: replaced pos:noun → pos:verb`. |
| `/unlabel <word> <spec>...` | Detach the named labels from a word. Same spec syntax as `/label`. Tokens that aren't attached (or whose label doesn't exist) are silently skipped; reply lists the names actually removed, or `nothing to remove`. |
| `/labels` | List every label in this chat with its attached-word count, alphabetically. Empty chat → `No labels yet.` Labels with no attached words still appear with `(0)`. |
| `/status` | Host diagnostics (hardware, OS, deployed commit short SHA, load, temp, disk free), vocab count for the chat, LLM endpoint/health, and a short model bench (chars + tok/s, `model not responding` on 30 s timeout). The deploy SHA is read from `/var/lib/teach-me-eng-bot/deploy.json` (written by the auto-deploy workflow); shows `unknown` when the manifest is missing. |

Plain (non-slash) messages go through the **just-talk** flow: the chat history is passed to the model with the current vocab list injected into the system prompt as soft hints. Any vocab words that appear literally in the reply bump `mention_count` and update `last_used_at`.

Scheduled **pushes** send 1 short snippet using 1 vocab word at a time, in the chosen tone. Each push message has `✅ knew / ❌ forgot` buttons; tapping either applies the corresponding FSRS rating (`Good` / `Again`).

## Architecture

Code is split into focused modules (entrypoint is `bot.py`):

- **`bot.py`** — python-telegram-bot wiring. Command/callback/message handlers, scheduler bootstrap, transcript/history management, DB connection lifecycle.
- **`llm.py`** — OpenAI-compatible HTTP client (local server or OpenRouter, selected by `LLM_BACKEND`). `stream_chat()` for live edits, `chat()` one-shot for pushes, `health()` and `bench()` for `/status`. SSE/completion parsing is factored into pure helpers.
- **`sysinfo.py`** — pure readers for host diagnostics used by `/status` (hardware, OS, load, temp, disk free). Each reader has an injectable dependency and a safe fallback so `/status` works on hosts that don't expose the underlying files.
- **`vocab.py`** — vocabulary CRUD, literal mention scanning, FSRS rating (`rate_word`), and the weighted-random `select_word`. Uses `py-fsrs` with `desired_retention=0.95` and `maximum_interval=7d` so review intervals stay tight.
- **`prompts.py`** — tone-flavoured push templates and the just-talk system-prompt composer that appends the chat's vocab as soft hints.
- **`config_flow.py`** — `Settings` dataclass + `ConfigSession` state machine for `/start`; per-step validators (IANA tz, 6–12 pushes, HH:MM, known tone, known target language via `translator.normalize_target`); `save_settings` / `load_settings` upsert against the `chats` table.
- **`translator.py`** — thin wrapper around `deep_translator.GoogleTranslator` for `/translate`. `normalize_target` maps a name or ISO code to an ISO code (no network); `translate(text, target, source='auto')` does the Google call; `is_target_script(text, target)` decides reverse-translate intent by Unicode script; `vocab_target` picks the English side of the pair (source for forward, translation for reverse); `format_vocab_note` builds the 1-line vocab-add status shown inline when the 5-word cap is exceeded; `PendingVocab` is the short-token↔word registry backing the `➕ Add to vocab` button. Explicitly bypasses the LLM because small free-tier models translate weakly into non-English scripts.
- **`scheduler.py`** — `plan_push_times` (equal-bucket sampling with half-gap edge buffers), `compose_push` (select + LLM call + retry once if the word didn't appear literally), `log_push` / `mark_rated`, and `PushRunner` wrapping `AsyncIOScheduler` with per-chat daily re-planning at 00:01 local.
- **`db.py`** — SQLite schema (`chats`, `words`, `push_log`) with FSRS columns on `words`; `connect()` sets WAL + `PRAGMA foreign_keys=ON`; `init_db()` applies forward-only column migrations.
- **`games.py`** — pure scaffolding for `/games`: `Round`/`Game` dataclasses, `draw_rounds(rows, *, direction="wt"|"tw", ...)` (sample `min(10, N)` correct words + 3-distractor option sets per round, both without replacement; the `direction` kwarg picks whether the prompt is the English text and options are translations, or vice versa), `apply_answer` (mutates score and current_round), `format_result`. No telegram imports; `bot.py` does the wiring and holds the per-chat in-memory map.
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
  - `chats(chat_id PK, tz, pushes_per_day, active_start, active_end, tone, translate_target, created_at)`
  - `words(id PK, chat_id FK, text, added_at, mention_count, last_used_at, stability, difficulty, state, step, due, reps, lapses, last_review, UNIQUE(chat_id, text))`
  - `push_log(id PK, chat_id, sent_at, tg_message_id, word_ids_json, rated)`
- Cascade-delete: removing a chat drops its words and push_log entries.

## Logs on disk

- `logs/bot.log` — rotating file log (5 MB × 5 backups), mirrors journald output. `httpx` / `telegram` / `apscheduler` loggers are pinned to WARNING so the bot token never appears in request URLs.
- `logs/convs/<chat_id>/NNN.txt` — human-readable transcript, one file per conversation, zero-padded sequential numbering per chat. Push messages are included tagged `push`.
- `logs/` and `data/` are git-ignored.

## Key constants

- `bot.py`: `EDIT_INTERVAL = 2.0` (seconds between stream edits), `MAX_MSG_LEN = 4000` (per-message cap; long responses spill into additional messages).
- `llm.py`: `LLAMA_URL`, `MODEL = "gemma4"` (model id sent to the local backend; OpenRouter uses `OPENROUTER_MODEL`).
- `vocab.py`: `FSRS_RETENTION = 0.95`, `FSRS_MAX_DAYS = 7`, `RECENCY_TAU_DAYS = 7.0`.
- `scheduler.py`: `MIN_GAP_MIN = 45` (minimum spacing between consecutive pushes, enforced implicitly by the bucket algorithm).

## Agent fabric

Day-to-day changes here are driven from issues by an external agent pipeline maintained in [`valdisd96/agent-fabric`](https://github.com/valdisd96/agent-fabric). The fabric owns the `state:*` label workflow, the `plan-exec` → `test-writer` → `review-pr` skills, and the cross-project orchestrator. The local `.claude/skills/` directory is rendered from there; see that repo's `DESIGN.md` for the architecture and `workflow.md` here for the state machine.
