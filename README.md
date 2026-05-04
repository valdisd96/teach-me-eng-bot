# teach-me-eng-bot

A Telegram bot that helps you learn English vocabulary.

You save words with `/add`. The bot sends short tone-flavoured push messages that use those words, and `✅ / ❌` buttons update an FSRS spaced-repetition schedule per word. Plain (non-slash) messages stream a chat reply from an LLM with your vocab injected as soft hints.

A [python-telegram-bot](https://python-telegram-bot.org) app talking to any OpenAI-compatible chat-completions endpoint. Per-chat vocabulary, settings, and FSRS state live in SQLite (`data/vocab.db`); APScheduler sends randomised pushes inside each chat's active window.

---

## Commands

| Command | Purpose |
|---|---|
| `/start` | Guided config: timezone → pushes/day (6–12) → active window → tone (funny / motivational / scary / bright / mixed) → translate target language. Re-running overwrites settings; vocab is preserved. |
| `/help` | Lists all commands with descriptions. |
| `/clear` | Resets the chat's LLM history. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. |
| `/remove <word or phrase>` | Remove by exact match. |
| `/list [substring]` | List vocab (least-mentioned first), optionally filtered. |
| `/import` | Bulk-import from a CSV upload (5-min window, capped at 5000 rows / 1 MB). One word per row in the first column; optional `text` header. Existing words preserved; in-file duplicates skipped. FSRS state not imported. |
| `/export` | Sends the chat's vocab back as `vocab-YYYY-MM-DD.csv`, alphabetical, with a `text` header. FSRS state not exported. |
| `/resetvocab` | Wipe vocabulary (with a confirm button). |
| `/translate <text>` | Google-translates args (or replied message) into the chat's target language. Reverse-translates non-Latin input back to English. Inline `➕ Add to vocab` button on results ≤5 words. Bypasses the LLM. |
| `/status` | Host diagnostics, vocab count, LLM endpoint health, short bench. |

Plain (non-slash) messages hit the LLM with vocab injected into the system prompt as soft hints. Words that appear literally in the reply bump their `mention_count` and get freshness credit.

Scheduled pushes send 1 short snippet using 1 vocab word in the chosen tone, with `✅ knew / ❌ forgot` buttons that apply FSRS `Good` / `Again`.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN
python bot.py
```

For tests: `pip install -r requirements-dev.txt && python -m pytest -q`.

### As a systemd service

```bash
sudo bash install-service.sh
```

Copies `teach-me-eng-bot.service` to `/etc/systemd/system/`, enables it on boot, starts it.

```bash
systemctl status teach-me-eng-bot
journalctl -u teach-me-eng-bot -f
systemctl restart teach-me-eng-bot
```

---

## Environment variables

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | friendly-tutor default in `.env.example` |
| `ALLOWED_USER_IDS` | No | empty (allow all) — comma-separated Telegram user IDs |
| `LLM_BACKEND` | No | `llama` (local OpenAI-compatible server on `127.0.0.1:8080`); set to `openrouter` for the cloud backend |
| `OPENROUTER_API_KEY` | When `LLM_BACKEND=openrouter` | — |
| `OPENROUTER_MODEL` | No | a free-tier OpenRouter model — see `.env.example` |

Per-chat scheduling (timezone, pushes/day, active window, tone, translate target) lives in SQLite, populated by `/start`. Not env vars.

---

## Architecture

Per-module breakdown lives in **[`CLAUDE.md`](CLAUDE.md)**. Code is split into `bot.py` (Telegram wiring) plus dedicated modules for `llm`, `vocab`, `prompts`, `config_flow`, `scheduler`, `translator`, `sysinfo`, and `db`.
