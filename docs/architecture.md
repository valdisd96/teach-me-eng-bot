# Architecture

`teach-me-eng-bot` is a single-process Telegram bot with explicit module seams.

## Runtime shape

- `bot.py` wires Telegram handlers, application startup, command routing, callbacks, and scheduler startup.
- `db.py` owns SQLite connection and idempotent schema setup.
- `vocab.py` owns vocabulary CRUD, labels, FSRS-ish state, and selection helpers.
- `scheduler.py` owns per-chat push scheduling and due-word selection.
- `llm.py` owns OpenAI-compatible chat completions / OpenRouter calls.
- `translator.py` owns translation behavior for `/tr`.
- `games.py`, `repeat_game.py`, `focus_drill.py`, and `irregular_verbs.py` own game state and grading.
- `config_flow.py` owns `/start` setup conversation.
- `prompts.py` owns tutor and snippet prompt construction.
- `sysinfo.py` owns `/status` host/model diagnostics.

Persistent state is local SQLite under `data/`; runtime secrets live in `.env` loaded by systemd.

## Important seams

- Telegram API access should stay behind handler/application boundaries so tests can use fake contexts and updates.
- LLM and translator calls should be mockable; do not make routine tests depend on live external APIs.
- SQLite schema changes belong in `db.py::init_db()` and must be additive/idempotent unless a human-approved migration plan exists.
- Scheduler logic should stay deterministic enough to test without sleeping or waiting on wall-clock time.

## Protected architecture areas

Changes require extra care and usually human approval if they touch:

- `.github/workflows/deploy.yml` or deployment topology;
- `teach-me-eng-bot.service` / systemd behavior;
- `.env` parsing, token use, or access control (`ALLOWED_USER_IDS`);
- data deletion/reset behavior;
- DB migration semantics;
- provider/backend cost behavior.
