# Testing and validation

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## Full test suite

```bash
.venv/bin/python -m pytest -q
```

## Compile check

```bash
.venv/bin/python -m compileall -q .
```

## Smoke subset

Use this when a full run is too expensive but deployment/runtime confidence is needed:

```bash
.venv/bin/python -m pytest tests/test_status_command.py tests/test_scheduler.py -q
```

## Test conventions

- Prefer tests before code for behavior changes.
- Keep Telegram-facing text changes covered by focused tests when practical.
- For DB/schema changes, prove `init_db()` is idempotent and preserves existing data.
- For scheduler changes, include deterministic tests for active windows, counts, and due-word selection.
- For LLM/OpenRouter changes, mock network calls; do not require live provider calls in CI.

## Factory handoff expectations

Worker handoffs should include:

- tests run, with exact command and result;
- whether docs were updated or why they were not needed;
- risk classification;
- any protected paths touched;
- any remaining manual verification needed.
