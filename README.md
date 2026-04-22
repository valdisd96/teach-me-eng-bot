# gemma-rpi-agent

A Telegram bot that streams responses from a local **Gemma 4** model running via [llama.cpp](https://github.com/ggerganov/llama.cpp)'s OpenAI-compatible HTTP server on a Raspberry Pi, **plus an FSRS-driven English-vocabulary agent** that sends short tone-flavoured push messages using your saved words and tracks per-word memory state via rating buttons.

Chat replies are streamed live by editing a placeholder message; the footer of each reply shows current CPU load and temperature. Scheduled pushes use a non-streaming one-shot call.

## Requirements

- Raspberry Pi (tested on RPi with 64-bit kernel)
- Python 3.10+
- [llama.cpp](https://github.com/ggerganov/llama.cpp) server running with a Gemma 4 model on `http://127.0.0.1:8080`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN
```

For running tests: `pip install -r requirements-dev.txt && python -m pytest -q`.

## Running manually

```bash
source .venv/bin/activate
python bot.py
```

The llama.cpp server must be running before starting the bot.

## Running as a systemd service

```bash
sudo bash install-service.sh
```

This copies `gemma-rpi-agent.service` to `/etc/systemd/system/`, enables it on boot, and starts it immediately.

Useful commands:

```bash
systemctl status gemma-rpi-agent
journalctl -u gemma-rpi-agent -f   # live logs
systemctl restart gemma-rpi-agent
```

## Environment variables

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | `"You are a friendly English tutor chatting casually with a learner. Use natural, everyday English. If they ask about grammar, vocabulary, or usage, explain briefly with a small example."` |
| `ALLOWED_USER_IDS` | No | empty (allow all) |

Per-chat scheduling settings (timezone, pushes/day, active window, tone) are collected via the `/start` flow and stored in SQLite at `data/vocab.db` — not via env vars.

## Bot commands

| Command | Description |
|---|---|
| `/start` | Walks a guided config: timezone → pushes/day (6–12) → active window (HH:MM) → tone (funny/motivational/scary/bright/mixed). Re-running overwrites settings; vocab is preserved. |
| `/clear` | Resets the chat's LLM history. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. |
| `/remove <word or phrase>` | Remove a word. |
| `/list [substring]` | List vocab (least-mentioned first), optionally filtered by substring. |
| `/resetvocab` | Wipe the chat's vocabulary (with a confirm button). |
| `/model` | Show the llama.cpp endpoint and health status. |

Plain-text messages go to the chat model with your vocab injected into the system prompt as soft hints. Words that appear literally in the reply bump their mention count.

Scheduled pushes arrive inside your active window at randomized times. Each carries `✅ knew / ❌ forgot` buttons that apply FSRS `Good` or `Again` ratings to the highlighted word.

## Architecture

See [`CLAUDE.md`](CLAUDE.md) for the full module breakdown. Code is split into `bot.py` (Telegram wiring) plus dedicated modules for `llm`, `vocab`, `prompts`, `config_flow`, `scheduler`, and `db`. SQLite (`data/vocab.db`) holds per-chat settings, vocabulary + FSRS state, and push history.
