# AGENTS.md — Hermes Dark Factory onboarding

This repo is managed by Valdis's Hermes Dark Factory as project `teach-me-eng-bot`.

## What this project is

`teach-me-eng-bot` is a Telegram English-tutor bot. It stores per-chat vocabulary/settings in SQLite, schedules FSRS-style vocabulary pushes, runs typed/inline learning games, translates short text, and streams casual tutor replies through an OpenAI-compatible LLM backend.

## Factory workflow

Default pipeline for issues and changes:

1. `project-planner` classifies risk, writes acceptance criteria, and creates downstream tasks.
2. `test-writer` adds or updates regression tests first when practical.
3. `implementer` works in an isolated worktree/branch and opens or updates a PR.
4. `code-reviewer` independently reviews spec compliance and factory-policy compliance.
5. `docs-keeper` updates repo docs and Drive-backed Obsidian notes when behavior/ops changes.
6. `merge-babysitter` watches CI and merge readiness. Low-risk validated changes may auto-merge; protected changes require Valdis approval.

Do not let the same agent be the only implementer and reviewer.

Every code change must follow Git flow: isolated worktree/branch → tests → PR → independent review and required gates → merge to `main` → deploy from the merged `main` SHA. This has no hotfix exception: emergency, self-healing, one-line, and operator-authored fixes must never be committed or pushed directly to `main`.

## Commands

Setup:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Tests:

```bash
.venv/bin/python -m pytest -q
```

Compile check:

```bash
.venv/bin/python -m compileall -q .
```

Smoke subset:

```bash
.venv/bin/python -m pytest tests/test_status_command.py tests/test_scheduler.py -q
```

## Deployment/runtime

Runtime VPS:

- host: `root@178.105.242.161`
- SSH key: `/root/.ssh/id_ed25519_home`
- path: `/root/teach-me-eng-bot`
- service: `teach-me-eng-bot.service`
- process: `/root/teach-me-eng-bot/.venv/bin/python bot.py`

The service loads `/root/teach-me-eng-bot/.env`. Never print or commit token/API values.

Persistent runtime state that must not be wiped:

- `/root/teach-me-eng-bot/.env`
- `/root/teach-me-eng-bot/data/vocab.db` and WAL/SHM siblings
- `/root/teach-me-eng-bot/logs/`
- `/root/teach-me-eng-bot/.venv/` except during intentional dependency refresh

## Human gates

Human approval is required before:

- changing tokens, environment values, or credential handling;
- auth/session/security changes;
- billing/provider-cost changes;
- destructive DB/file operations or migrations with data-loss risk;
- systemd service/timer or production/deployment infrastructure changes;
- GitHub Actions deploy workflow changes;
- changes to Telegram bot identity, allowed users, or access controls;
- large architecture shifts or weakly tested code changes.

High-risk issues should still be planned and prepared safely; only irreversible steps are gated.

## Documentation expectations

Update these repo docs when relevant:

- `docs/factory.md` — Dark Factory contract and autonomy policy.
- `docs/testing.md` — validation commands and test conventions.
- `docs/deploy.md` — runtime/deployment contract.
- `docs/architecture.md` — module map and seams.
- `AGENTS.md` — agent onboarding instructions.

For operational changes, also update the Drive-backed Obsidian project notes under `Hermes/02_Projects/Hermes Dark Factory/Teach Me Eng Bot/`.
