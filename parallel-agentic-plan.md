# Parallel Agentic Workflow — Implementation Plan

Adapts `common-fabric-plan.md` for this repo (Python Telegram bot + llama.cpp + FSRS vocab).
Goal: 2 parallel Claude Code agents working in isolated git worktrees on GitHub issues, coordinated via a label-based state machine on those issues, with AI pre-review before manual merge.

## Decisions

| # | Decision |
|---|---|
| 1 | **2 parallel agents** |
| 2 | **Incremental rollout**: Phase 1 (isolation) + Phase 3 (parallel impl) + Phase 4 (review) first. Phase 5 (self-heal) deferred. |
| 3 | **2 test bot tokens** (Bot1, Bot2), pinned per worktree slot |
| 4 | **OpenRouter `google/gemma-4-26b-a4b-it:free`** for pre-merge live testing (bypasses the Pi's llama.cpp) |
| 5 | **GitHub Issues** are the unit of work |
| 6 | **Planner session always intermediates**: user-written issues still get decomposed via a `plan-issue` skill |
| 7 | **Custom `review-pr` skill** (tailored to `dev-flow` rules), with `/ultrareview` as an opt-in deeper review when needed |
| 8 | **No adversarial review** in v1 |
| 9 | **Nested** `./worktrees/<branch>/` (gitignored) |
| 10 | **iTerm tabs + spawn script**, not Archon (revisit if scale grows) |

## Label state machine

Labels become the shared protocol between user, planner, and agents.

### Workflow state (one-of, flips as the issue moves)

| Label | Set by | Meaning |
|---|---|---|
| `state:needs-planning` | User / default on create | Raw issue, planner hasn't touched it |
| `state:ready-for-parallel-work` | Planner | Scoped, self-contained, unambiguous — safe for an agent to pick up |
| `state:in-progress` | Spawn script | An agent has claimed it |
| `state:clarification-needed` | Agent (via `clarify-issue` skill) | Agent posted a comment, awaiting user input |
| `state:ready-for-review` | `review-pr` skill when PR passes | Waiting for user's manual merge |
| `state:needs-rework` | `review-pr` skill when PR fails | Back to the agent |
| `state:blocked` | Anyone | Waiting on another issue — include ref in comment |

### Type (maps 1:1 to `dev-flow` branch prefixes)

`type:feat`, `type:fix`, `type:chore`, `type:refactor`

### Priority (planner assigns)

`priority:high`, `priority:medium`, `priority:low`

### Area (optional, navigational)

`area:bot`, `area:vocab`, `area:scheduler`, `area:llm`, `area:translator`, `area:config`, `area:db`

### Hints (planner sets, spawner reads)

- `touches:bot.py` — don't schedule two of these concurrently (avoids wiring-file merge conflicts)

### Agent-selection rule

```bash
gh issue list \
  --label state:ready-for-parallel-work \
  --label priority:high \
  --limit 1
```

## Tasks — one PR per task

### Task 1 — Worktree infrastructure

**Status:** Done in [#32](https://github.com/valdisd96/gemma-rpi-agent/pull/32). Implementation notes worth carrying into later tasks:
- Slot number is recorded in `<git-dir>/wt-slot` (the worktree's own git metadata directory), not in the working tree. Future scripts that want to read the slot of a worktree at `<path>` should do `cat "$(git -C <path> rev-parse --absolute-git-dir)/wt-slot"`.
- `wt.sh create` always cuts `-b <branch>` from `main` — Task 4's spawn script can rely on this.
- `data/` is created empty; `vocab.db` is materialised by the bot on first run, so no per-worktree DB seeding is needed.
- If `env/slot<N>.env` is missing, `wt.sh` seeds it from `.env.example`. Task 2 should overwrite/extend that file with real `TELEGRAM_TOKEN` + `OPENROUTER_API_KEY` values rather than recreate it.
- `.gitignore` was tightened from `.venv/` to `.venv` so the symlink form drops out of `git status` inside worktrees. Anything else wt.sh adds to a worktree is either gitignored (`.env`, `.venv`, `data/`) or kept outside the working tree (`<git-dir>/wt-slot`).
- `read -p` only echoes prompts on a TTY; the destroy script prints its warnings to stderr ahead of the read so non-interactive callers (CI, agent scripts) still see the reason for a failed prompt.

**Deliverables**
- `scripts/wt.sh create <branch> [slot]` — `git worktree add ./worktrees/<branch> -b <branch> main`, empty `data/` dir, `.venv` symlink to main `.venv` (fast), copies `env/slot<N>.env` → `.env`.
- `scripts/wt.sh destroy <branch>` — prompts if branch has uncommitted/unpushed work; then `git worktree remove` + cleanup.
- `scripts/wt.sh list` — wraps `git worktree list` with slot info.
- `.gitignore`: add `worktrees/` and `env/slot*.env`.

**Risk:** Python `.venv` isn't trivially portable across worktrees. Symlink approach verified in #32 — works for this repo because none of the deps in `requirements.txt` bake absolute paths into installed scripts/wheels.

### Task 2 — Dual-backend LLM + token pool

**Status:** Code merged in [#33](https://github.com/valdisd96/gemma-rpi-agent/pull/33). Implementation notes to carry forward:
- Backend selection lives in `llm._get_backend()`, which reads `LLM_BACKEND`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` from env on each call (not at import). Anything that needs to know the current backend should call this helper rather than re-derive it from env.
- `LLM_BACKEND` matching is case-insensitive on the string `openrouter`. Anything else (including unset, empty, typos) falls back to the llama.cpp path — keeps the live Pi deployment immune to env-var noise.
- Missing `OPENROUTER_API_KEY` while `LLM_BACKEND=openrouter` raises `RuntimeError` lazily on the first LLM call, not at startup. `bot.py` is intentionally not coupled to backend validation.
- `health()` short-circuits for OpenRouter and returns `"openrouter (model=<name>)"` without a network round-trip — kept `/status` snappy and avoided burning the auth-key probe quota.
- Slot env files (`env/slot1.env`, `env/slot2.env`) remain gitignored and per-machine. `wt.sh` from #32 seeds them from `.env.example`, so each new worktree starts with the right schema (TELEGRAM_TOKEN + the three OpenRouter keys) ready to fill in.
- Tests use `httpx.MockTransport` patched onto `httpx.AsyncClient` via `monkeypatch` — useful pattern when Task 4/5 code needs to assert request shape without standing up a fake server.

**Deliverables**
- `env/slot1.env`, `env/slot2.env` (gitignored) each with its `TELEGRAM_TOKEN` + `OPENROUTER_API_KEY`.
- `llm.py`: tiny dispatch — when `LLM_BACKEND=openrouter`, call `https://openrouter.ai/api/v1/chat/completions` with `model=google/gemma-4-26b-a4b-it:free`; otherwise current llama.cpp path.
- `.env.example`: add `LLM_BACKEND`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, documented in `CLAUDE.md`.
- Smoke: `python bot.py` with `LLM_BACKEND=openrouter` answers a real Telegram message. (Manual; deferred — needs a real Telegram + OpenRouter pair.)

**Risks**
- OpenRouter free-tier model differs from Gemma-4 on the Pi; responses will diverge. Use for smoke only, not prompt-tuning.
- Free-tier rate limits — not load-tested in #33; default model is `google/gemma-4-26b-a4b-it:free`, swap via `OPENROUTER_MODEL` if quotas bite.

### Task 3 — Label taxonomy + planner/clarify skills

**Status:** Done in [#34](https://github.com/valdisd96/gemma-rpi-agent/pull/34). 22 labels live on the repo. Implementation notes for Task 4/5:
- `scripts/setup-labels.sh` uses `gh label create --force`, so it's safe to re-run after schema edits — it updates colour/description in place.
- Skills are tracked under `.claude/skills/` and auto-load in any Claude Code session opened against this repo. Worktrees inherit them via the normal checkout, so a worker agent in a worktree gets `clarify-issue` for free.
- `plan-issue` deliberately **never** sets `state:in-progress` — that's the spawn script's job in Task 4. Same for `state:ready-for-review` / `state:needs-rework` (Task 5's `review-pr` owns those). The skill's "Hard rules" section enforces this.
- Children created during decomposition stay at `state:needs-planning` (not `state:ready-for-parallel-work`) — each child gets its own planning pass so the user can re-scope before any agent picks them up.
- `clarify-issue` requires the issue to already be `state:in-progress`; it explicitly forbids being invoked outside an active work session, and forbids speculative parallel work after the comment is posted.
- `CLAUDE.md` got a short pointer at the workflow; full taxonomy stays here in the plan (one source of truth, easy to edit in one place).
- The `openrouter/free` auto-router triggered a real bug in `llm._parse_completion` during smoke testing — `content: null` from reasoning-only models crashed `bench()`. Fixed in the same PR (`6b8731f`); regression test added.

**Deliverables**
- `scripts/setup-labels.sh` — idempotent `gh label create` for every label above (re-runnable).
- `.claude/skills/plan-issue/SKILL.md` — given an issue number, read body, ask clarifying questions inline only if truly ambiguous; otherwise decompose:
  - "One unit of work" → label `state:ready-for-parallel-work` + `type:*` + `priority:*` + any `area:*` / `touches:bot.py`.
  - "Multiple units" → create child issues (`gh issue create`), link back to parent, mark parent `state:blocked`.
- `.claude/skills/clarify-issue/SKILL.md` — when an agent hits genuine ambiguity, post a comment with the specific question and flip label to `state:clarification-needed`. Agent then stops and waits.
- `CLAUDE.md`: document the state machine.

**Risk:** Planner over-decomposing tiny issues. Skill needs a firm "prefer one issue when split-cost > benefit" rule. Mitigated in `plan-issue/SKILL.md` via the explicit "Anti-decomposition rule" and three worked examples (single, multi, ambiguous).

### Task 4 — Spawn script & in-worktree `work-issue` skill
**Deliverables**
- `scripts/spawn-agent.sh <issue-number> [slot]` — resolves issue, creates worktree via `wt.sh`, symlinks slot env, opens iTerm tab with `claude` starting from a prompt like *"Work issue #N in this worktree. Use the `work-issue` skill."*
- `scripts/spawn-fleet.sh` — queries `gh issue list --label state:ready-for-parallel-work` sorted by priority, spawns up to 2. Enforces "no two `touches:bot.py` issues at once".
- `.claude/skills/work-issue/SKILL.md` — in-worktree loop: flip label to `state:in-progress` at start, follow `dev-flow` (branch already created by spawner), implement, test, open PR with body ending `Closes #N`.

**Risk:** `bot.py` merge conflicts when both agents touch wiring — mitigated by the `touches:bot.py` hint + spawner rule. Accept occasional conflicts otherwise.

### Task 5 — `review-pr` skill
**Deliverables**
- `.claude/skills/review-pr/SKILL.md` — in a **fresh session** (emphasize `/clear` before invoking): read PR, read linked issue, read the diff, run `pytest`, check `dev-flow` rules as a checklist:
  - Branch prefix matches `type:*` label
  - Tests cover new code paths
  - `.env.example` updated if new env var
  - `bot.py` changes are wiring-only
  - Docstring/type-annotation style matches the repo
  - Issue scope actually addressed (compare PR body to issue body)
- Post review via `gh pr review --comment` (or `gh pr comment`), flip issue label to `state:ready-for-review` or `state:needs-rework`.
- **Never** auto-merges — user merges manually (per `dev-flow`).

**Risk:** Same-model reviewer = limited independence. Accepted trade-off; `/ultrareview` stays as the escape hatch for risky PRs.

### Task 6 — Stitch-up: docs + optional refactor
**Deliverables**
- `CLAUDE.md`: add a "Parallel workflow" section with an ASCII state-machine diagram.
- Assess whether `bot.py` needs a light refactor (e.g. move command registration into a small `handlers/` package) to reduce collision surface — only if Task 4 revealed real pain.
- (Optional) `/loop` or cron skill to auto-spawn when `state:ready-for-parallel-work` issues exist and slots are free. Phase-5-adjacent; skip unless we feel the need.

## Execution order

```
[1: worktree] ── [2: LLM+tokens] ──┐
                                   ├── [4: spawn+work-issue] ── [5: review-pr] ── [6: stitch]
[3: labels+planner] ───────────────┘
```

Tasks 1 and 3 are independent and can run in parallel (nice dogfood moment once Task 1 lands). Task 2 depends on Task 1 (worktree layout informs where slot envs live). Task 4 needs 1+2+3. 5 needs 4. 6 last.

## Risks & trade-offs summary

1. **`bot.py` merge conflicts** — planner-level `touches:bot.py` hint + spawner rule. Accept residual conflicts as the cost of parallelism.
2. **OpenRouter ≠ llama.cpp** — different model, different outputs. Smoke-test only; not a substitute for Pi-side validation.
3. **Same-model reviewer** — adversarial skipped; `/ultrareview` is the escape valve.
4. **Free-tier rate limits** — both agents hammering OpenRouter may queue. Fine for hobby scale.
5. **Telegram polling** — current mode is fine. If webhooks are ever adopted, each test bot would need a distinct URL; noted.

## Out of scope for v1

- Phase 5 self-healing loop (agent amends `CLAUDE.md` when it drifts) — revisit after Tasks 1–6 have been exercised on real PRs so we can target the actual drift patterns.
- Database branching à la Neon — SQLite file per worktree is sufficient.
- Adversarial second-opinion review.
- Archon or any external orchestrator — revisit if scale grows past 2 agents or the bash glue feels fragile.
