---
name: dev-flow
description: This skill should be used when starting any code change in this repo — enforces the branch → change → commit → PR → wait process. Invoke when the user asks to "fix", "add", "implement", "refactor", or make any modification to bot.py, requirements.txt, .env.example, or other project files.
version: 1.0.0
---

# dev-flow

The process for every code change in this repo. No exceptions unless the user explicitly says so.

## The flow

1. **Start from fresh main**

   ```bash
   git checkout main && git pull --ff-only
   ```

2. **Cut a branch** with a type prefix and a short kebab-case topic:

   - `feat/<topic>` — new behavior (new command, new config option, etc.)
   - `fix/<topic>` — bug fix
   - `chore/<topic>` — deps, docs, service files, non-code changes
   - `refactor/<topic>` — no behavior change

3. **Make the change.** Keep the diff focused on one concern — don't bundle unrelated edits.

4. **Smoke-check before committing:**

   ```bash
   python -m py_compile bot.py   # syntax check — always run
   ```

   If there are tests in `tests/`, run them too:
   ```bash
   source .venv/bin/activate && python -m pytest -q
   ```

   Fix root causes — never skip.

5. **Commit.** Subject line ≤ 70 chars, imperative mood, body explains *why* not *what*.

6. **Push and open a PR against `main`:**

   ```bash
   git push -u origin HEAD
   gh pr create --base main --title "<title>" --body "<summary + test plan>"
   ```

7. **Wait.** The user merges manually — that is the approval gate. Never call `gh pr merge`.

8. **After merge**, clean up:

   ```bash
   git checkout main && git pull --ff-only && git branch -d <branch>
   ```

## Rules

- **Main is protected.** Never push directly to main. Never force-push to main.
- **`bot.py` is the whole app.** Changes to constants (`LLAMA_URL`, `MODEL`, `EDIT_INTERVAL`, `MAX_MSG_LEN`) or handler logic can break the live bot — mention the impact in the PR body.
- **`.env` is never committed.** Secrets live only in `.env`. If adding a new env var, add it to `.env.example` and document it in `CLAUDE.md`'s environment table.
- **New bot commands need three surfaces updated.** When adding (or renaming/removing) a slash command, update `COMMANDS` in `bot.py` (drives both `/help` output and Telegram's `set_my_commands` autocomplete), the `HELP_TEXT` "Getting started" prose if the user-facing flow changes, and `CLAUDE.md`'s "Bot commands" table. Skipping any of these leaves the feature undiscoverable in the bot UI even though the handler works.
- **Service files need a note.** If touching `gemma-rpi-agent.service` or `install-service.sh`, call it out in the PR — these affect the systemd-managed deployment on the Pi.
- **Cover all functionality with tests.** Every new code path, new function, or bug fix needs a test that would fail without your change. If the new logic sits in a hard-to-test layer (e.g. a Telegram handler in `bot.py`), refactor the testable piece out into a pure helper module (`scheduler.py`, `vocab.py`, `prompts.py`, etc.) and test it there — don't leave behavior uncovered just because the entrypoint is awkward to mock. The only acceptable gap is genuinely glue code whose only job is to wire tested helpers to an external framework.
- **Code style — structured, not clever.** Match the existing module layout (`bot.py` is wiring only; domain logic lives in `llm.py`, `vocab.py`, `prompts.py`, `scheduler.py`, `config_flow.py`, `db.py`). Prefer small focused modules over growing `bot.py`. Keep pure helpers separate from I/O: logic that can be expressed as a function of its arguments should not open HTTP clients, hit SQLite, or touch Telegram — pass what it needs in, return what it computes out (see `plan_push_times`, `compute_weight`, `scan_mentions`, `_parse_sse_delta` as patterns to follow). Avoid deep nesting — flatten with early returns / guard clauses instead of stacking `if/else`. Inject collaborators (`llm_chat`, `rng`, `now`, `dispatch`) as keyword args with sensible defaults so tests can substitute fakes without monkeypatching. Keep function signatures small and explicit; reach for a `dataclass` (like `Settings`) before a bag of positional args. Type-annotate everything public, use `from __future__ import annotations`, and match the existing docstring style: one line summarizing *why*, not mechanical restatements of the signature.
- **The user decides when to merge.** Never auto-merge.
- **Don't squash or rebase published commits** without being asked.

## When to deviate

Only if the user explicitly says so — e.g. "just commit to main", "skip the PR". Note the deviation in the conversation.
