# Deploy contract

This document is the canonical reference for how `teach-me-eng-bot` runs in production. The fabric's `deploy-diagnose` skill reads this file when it's triaging a deploy failure — keeping it accurate makes diagnoses faster and more precise. Hand-maintain it as part of any change that touches deploy surface (a new env var, a new system dep, a new background process).

## Overview

The bot is a single long-running Python 3.12 process that polls Telegram via long-polling and (by default) talks to a remote OpenAI-compatible chat completions endpoint. SQLite is the only persistent store. There are no other services, no message broker, no reverse proxy.

```
┌───────────────────────────────┐
│  teach-me-eng-bot.service     │
│  systemd unit, single process │
│                               │
│  python bot.py                │
│   ├─ python-telegram-bot       │  ──long-poll──▶  api.telegram.org
│   ├─ APScheduler (in-process) │
│   ├─ httpx                    │  ──HTTPS────▶   openrouter.ai (or local LLM)
│   ├─ deep-translator          │  ──HTTPS────▶   translate.google.com
│   └─ sqlite3                  │  ──fs──────▶   data/vocab.db
└───────────────────────────────┘
```

No inbound HTTP. No public ports. Outbound HTTPS only.

## Host requirements

- **OS**: Linux. Tested on Ubuntu 24.04. The systemd unit + python venv layout assumes a glibc-based distro.
- **Python**: 3.12 (or any version supported by the dependencies in `requirements.txt`). `python3 -m venv` is required.
- **System packages**: `python3.12`, `python3.12-venv`, `sqlite3` (for ad-hoc inspection; the bot itself uses the stdlib `sqlite3` module). Nothing else.
- **No native build deps** — all Python deps in `requirements.txt` ship as wheels.

## Service topology

A single systemd unit defined at `teach-me-eng-bot.service` (committed at the repo root):

```ini
[Service]
User=root
WorkingDirectory=/root/teach-me-eng-bot
EnvironmentFile=/root/teach-me-eng-bot/.env
ExecStart=/root/teach-me-eng-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
```

- **User**: `root`. The bot's working dir, venv, and SQLite live under `/root/`. A future PR may relocate to `/srv/teach-me-eng-bot` and switch to a non-root user; until then, the Hermes Dark Factory deploy reconciler SSHes in as root.
- **WorkingDirectory**: `/root/teach-me-eng-bot`. The bot's `Path(__file__).resolve().parent` (in `bot.py`) anchors all relative paths to this directory, so a relocation is mostly mechanical.
- **EnvironmentFile**: `/root/teach-me-eng-bot/.env` (mode 0600, owned by root, NOT in git). Loaded by systemd; the bot does not call `python-dotenv` from inside its working dir at runtime.
- **Restart**: `on-failure` — a clean exit (e.g. SIGTERM) does NOT auto-restart; a crash does. `RestartSec=5` means a tight crash loop is rate-limited.

## Environment variables

The bot reads these via `os.environ` (loaded by systemd's `EnvironmentFile=`). They are documented for users in the project README; this list is the *deploy* angle — what happens if each is missing or wrong.

| Variable | Required | Effect on startup |
|---|---|---|
| `TELEGRAM_TOKEN` | **Yes** | Unset or invalid → `python-telegram-bot` raises at the first `getMe` call within ~5s of process start. Look for `telegram.error.InvalidToken` or `Unauthorized` in journalctl. |
| `LLM_BACKEND` | No (default `llama`) | `openrouter` routes chat through OpenRouter (production); anything else falls back to a local server on `http://127.0.0.1:8080`. A wrong value with no local server gives connection-refused on the first chat. |
| `OPENROUTER_API_KEY` | When `LLM_BACKEND=openrouter` | Empty value with `LLM_BACKEND=openrouter` raises at the first LLM call (not at startup) — bot starts but every chat message fails. |
| `OPENROUTER_MODEL` | No | Defaults to a free-tier model id; an unsupported value 4xxs at the first chat call. |
| `SYSTEM_PROMPT` | No | Falls back to a sensible default; arbitrary string. |
| `ALLOWED_USER_IDS` | No | Empty = allow all. Comma/whitespace-separated Telegram user ids; non-numeric tokens are silently ignored. |

**Drift-checking convention**: when adding a new `os.environ.get(...)` or `os.getenv(...)` site to the codebase, mirror it in this table in the same PR. The diagnose skill cross-references journalctl errors against this table to identify "missing env var" failures fast.

## Persistent state

- **`/root/teach-me-eng-bot/data/vocab.db`** — primary SQLite database. Plus `vocab.db-shm` and `vocab.db-wal` (WAL-mode; survive restarts). Gitignored. **Must NOT be wiped on deploy.** The `git reset --hard` step in the deploy workflow does not touch gitignored files, so this is automatically preserved.
- **`/root/teach-me-eng-bot/.env`** — secrets. Mode 0600, owner root, gitignored. Survives deploys for the same reason.
- **`/root/teach-me-eng-bot/.venv/`** — Python virtualenv. Gitignored. Dependencies are re-installed only when `requirements.txt` changes (sha256 marker at `/var/lib/teach-me-eng-bot/requirements.sha256`, compared by the deploy reconciler).
- **`/root/teach-me-eng-bot/logs/`** — per-conversation transcripts (one file per chat, append-only). Gitignored; bounded only by chat volume. Manual rotation.
- **`/var/lib/teach-me-eng-bot/deploy.json`** — deploy manifest (`{sha, short_sha, deployed_at, deployer}`) written by the Dark Factory deploy reconciler on every deploy. `/status` reads it for the deployed commit SHA, and the reconciler's drift detection treats it as the source of truth for what is running (repo HEAD is only the bootstrap fallback).

## Migrations

There is **no `scripts/migrate.sh`**. SQLite schema changes happen inline in `db.py` via `init_db()` which runs at startup and is idempotent — a plain restart applies them.

If a future change needs an explicit pre-restart migration (e.g. a backfill that takes minutes and shouldn't run from inside `bot.py`), add `scripts/migrate.sh` (idempotent, exits non-zero on failure) and wire it into the factory's deploy config for this project (`deploy_after_merge` in the hermes-dark-factory registry). Update this section in the same PR.

## Deploy flow

Deploys are owned by the **Hermes Dark Factory deploy reconciler** (repo `valdisd96/hermes-dark-factory`, `watchers/deploy_reconciler.py`). There is no GitHub Actions deploy workflow — the former `.github/workflows/deploy.yml` (`github_actions_on_main_push`) was retired 2026-07-09: it required a self-hosted runner that was never installed, so pushes to `main` queued forever and never deployed.

Convergence contract: after **any** merge lands on `origin/main` — factory auto-merge, local merge + push, GitHub UI — production converges to that SHA within ≤15 minutes.

1. **Every 15 min** (and instantly after a factory-performed merge) the reconciler compares `origin/main` against the SHA in `/var/lib/teach-me-eng-bot/deploy.json`.
2. On drift it SSHes to the host and runs one chain: `git fetch/checkout main/pull --ff-only` (gitignored `.env`, `data/`, `.venv/`, `logs/` are untouched).
3. **Conditional dependency refresh**: `requirements.txt` sha256 is compared against `/var/lib/teach-me-eng-bot/requirements.sha256`; on change, `.venv/bin/pip install -r requirements.txt`.
4. `systemctl restart teach-me-eng-bot.service` (non-blocking).
5. **Health probe** (from the factory host, one retry): `systemctl is-active` + git/disk/journal checks via `scripts/teach_me_eng_vps_health.py`.
6. **On success** — the deploy chain wrote `/var/lib/teach-me-eng-bot/deploy.json` (`{sha, short_sha, deployed_at, deployer}`).
7. **On failure** — a self-healing incident is filed once per SHA (GitHub issue + release-diagnoser task) and an ACTION-NEEDED line lands in the project's Telegram topic; after 2 failed attempts the reconciler holds until `main` moves (no silent retry loops, no auto-rollback — the previous version keeps running until a fix-PR lands).

## Known failure modes

A grab-bag of failures that have happened or are easy to imagine. The diagnose skill cross-references against this list when triaging.

| Symptom in journalctl | Most likely cause | Fix |
|---|---|---|
| `telegram.error.InvalidToken` or `Unauthorized` at startup | `TELEGRAM_TOKEN` empty / rotated / pointed at the wrong bot | Re-fetch from `@BotFather`, update `/root/teach-me-eng-bot/.env`, `systemctl restart`. |
| `ConnectionError` to `127.0.0.1:8080` early in journal | `LLM_BACKEND=llama` (default) but no local LLM running | Either start the local LLM, or set `LLM_BACKEND=openrouter` + `OPENROUTER_API_KEY` in `.env`. |
| `httpx.HTTPStatusError 401` from OpenRouter at first chat | `OPENROUTER_API_KEY` rotated or empty | Refresh the key on openrouter.ai, update `.env`, `systemctl restart`. |
| `ModuleNotFoundError: <package>` at startup | `requirements.txt` updated but `.venv` not refreshed | The reconciler's hash-compare should catch this — if it didn't, delete `/var/lib/teach-me-eng-bot/requirements.sha256` to force a refresh on the next deploy, OR `.venv/bin/pip install -r requirements.txt` manually. |
| `sqlite3.OperationalError: no such table` | A code change references a table the schema doesn't have | Inspect `db.py::init_db()` — schema changes must be additive (CREATE TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN). DROP / rename are blocked by `safety.destructive_db_patterns` in `.fabric/config.yaml` and need explicit human override. |
| `OSError: [Errno 28] No space left on device` | `data/`, `logs/`, or systemd journal filling the disk | `df -h /root` to confirm. The bot's transcripts in `logs/` grow unbounded; rotate. SQLite WAL can also balloon under heavy write — `sqlite3 data/vocab.db 'PRAGMA wal_checkpoint(TRUNCATE);'` releases it. |
| Deploy succeeds but bot stops responding | The systemd unit may have started but failed to register webhooks / commands. Look for `telegram.error.NetworkError` or `Conflict: terminated by other getUpdates request`. The latter means a second instance is competing for long-polling — verify only one `bot.py` process is running. |

## What this contract does NOT cover

- **Zero-downtime deploys.** The `systemctl restart` step incurs a 2–5 second blip; for a single-user TG bot, this is acceptable. If multi-user load makes this matter, blue-green via two systemd units behind a routing layer is the path — not in scope today.
- **Backups.** `data/vocab.db` is not currently backed up off-host. A user data loss recovery story is a separate concern.
- **Multi-environment deploys** (staging, prod). One environment, one host, one deploy. Branching for env-specific config will need a second `.env` strategy.


## Dark Factory operations

Hermes Dark Factory monitors AND deploys this project as `teach-me-eng-bot`. The factory may inspect service health, Git state, disk usage, and recent journal output via SSH, may perform bounded recovery (`systemctl daemon-reload` + service restart) when the service is unhealthy, and owns the deploy reconciler described above.

Human approval is required before changing `.env`, service definitions, GitHub Actions deploy logic, destructive data operations, or deployment infrastructure.
