# gemma-rpi-agent

A Telegram bot that streams responses from a local **Gemma 4** model running via [llama.cpp](https://github.com/ggerganov/llama.cpp)'s OpenAI-compatible HTTP server on a Raspberry Pi.

Each reply is streamed live by repeatedly editing a placeholder Telegram message. The footer of every response shows current CPU load and temperature.

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
| `SYSTEM_PROMPT` | No | `"You are a helpful assistant running on a Raspberry Pi."` |

## Bot commands

| Command | Description |
|---|---|
| `/start` | Greet and reset conversation |
| `/clear` | Reset conversation history |
| `/model` | Show model name and llama.cpp server status |

## Architecture

All logic lives in `bot.py`:

- **`stream_llama(messages)`** — async generator that POSTs to the llama.cpp `/v1/chat/completions` SSE endpoint and yields token strings.
- **`handle_message()`** — appends user turn to per-chat history, sends a cursor placeholder, consumes tokens, and edits the placeholder every 2 s. Appends a system stats footer on the final edit.
- **`safe_edit()`** — wraps `message.edit_text` to absorb `RetryAfter` and `BadRequest: message is not modified`.
- **`sys_footer()`** — reads `os.getloadavg()` and `/sys/class/thermal/thermal_zone0/temp` to produce a live stats line.
- **`histories`** — in-memory `dict[int, list[dict]]` keyed by `chat_id`; standard OpenAI message format. Cleared on `/start` or `/clear`.
