# Issue-Driven Autonomous Workflow

A single-issue serial pipeline where three Claude agents — plan-exec, test-writer, reviewer — drive an issue from filed to merged. The user interacts only via GitHub issues and comments. An orchestrator daemon polls labels and dispatches the right agent.

## Goals

- The user's only inputs are: filing an issue, answering clarification comments, and (rarely) un-blocking a parked issue.
- All other state transitions happen automatically through label flips set by the agents.
- Auto-merge after reviewer approval. Bugs that ship are caught later by smoke tests and filed as new issues.

## Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  user files issue (defaults to state:needs-planning)                │
│       │                                                             │
│       ▼                                                             │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │ STAGE 1 — plan-exec                                           │ │
│  │ Skill: plan-exec                                              │ │
│  │ Context: full issue body + all comments + repo state          │ │
│  │                                                                │ │
│  │ At start: flip state:needs-planning → state:in-progress       │ │
│  │                                                                │ │
│  │ Decide: can I proceed without user input?                      │ │
│  │   NO  → invoke clarify-issue sub-skill                        │ │
│  │         post comment + flip state:clarification-needed        │ │
│  │         STOP (orchestrator will not re-fire this issue        │ │
│  │              until the label flips back)                      │ │
│  │   YES → cut branch, plan, implement, commit                   │ │
│  │         flip state:in-progress → state:tests-pending          │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                  │                                  │
│                                  ▼                                  │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │ STAGE 2 — test-writer  (FRESH session, no plan rationale)     │ │
│  │ Skill: test-writer                                             │ │
│  │ Context: PR diff (or branch diff if no PR yet) + tests/ dir   │ │
│  │                                                                │ │
│  │ Write tests covering the new code paths. Run pytest.          │ │
│  │   pass → push branch, gh pr create with `Closes #N` body      │ │
│  │          flip state:tests-pending → state:in-review           │ │
│  │   fail → comment on issue with the failure                    │ │
│  │          flip state:tests-pending → state:needs-rework        │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                  │                                  │
│                                  ▼                                  │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │ STAGE 3 — reviewer  (FRESH session)                           │ │
│  │ Skill: review-pr                                               │ │
│  │ Context: issue body + Q&A comments + full PR diff (incl tests)│ │
│  │                                                                │ │
│  │ Re-run pytest. Check the diff against the dev-flow checklist  │ │
│  │ AND the safety checks below.                                   │ │
│  │                                                                │ │
│  │ Approve → gh pr merge --squash --delete-branch                │ │
│  │           (issue auto-closes via Closes #N)                    │ │
│  │ Reject  → gh pr review --request-changes with comments        │ │
│  │           flip issue state:in-review → state:needs-rework     │ │
│  │           (Stage 1 picks it back up — cycle)                  │ │
│  │ Block   → flip state:blocked, comment why                     │ │
│  │           (sensitive content or cycle-limit hit; user only)   │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Three human gates only: **filing the issue**, **answering clarifications**, **un-blocking parked issues**.

### Plan comments (`<!-- agent-plan v1 -->`)

Before implementing, plan-exec posts a single issue comment whose body starts with the marker line `<!-- agent-plan v1 -->`. The comment captures *what plan-exec intended to do this cycle* — goal, approach, files to touch, anything explicitly out of scope. Each cycle posts a fresh comment (rework cycles do not edit older ones), so the issue accumulates a chronological history of intent.

Stage 2 (test-writer) reads the latest such comment for context — to understand what behaviours are being introduced and why this shape — but still treats the diff as the source of truth for what to test.

Stage 3 (reviewer) **deliberately filters these comments out**. Reviewing against the implementer's stated intent biases the verdict toward "did the agent do what it said it would" instead of "does the diff actually solve the issue." The reviewer judges only against the issue body, the human Q&A comments, and the diff itself.

## State labels

The full label set lives in `scripts/setup-labels.sh` (idempotent — safe to re-run).

### Workflow state (one-of, set by the agent that owns the transition)

| Label | Set by | Means |
|---|---|---|
| `state:needs-planning` | user (or implicit — fresh unlabelled issues are treated as if they had this) | Untouched. Stage 1 will pick this up. |
| `state:in-progress` | Stage 1 (plan-exec, at start) | Plan-exec is working. |
| `state:clarification-needed` | Stage 1 (clarify-issue) | **HUMAN GATE.** Question posted, waiting on user. Orchestrator skips. |
| `state:tests-pending` | Stage 1 (at end, after commit) | Branch + commit ready, awaiting test-writer. |
| `state:in-review` | Stage 2 (after tests green + PR opened) | Reviewer's turn. |
| `state:needs-rework` | Stage 2 (test failure) or Stage 3 (review failure) | Back to Stage 1. |
| `state:blocked` | Stage 3 or anyone manually | Stop. **HUMAN GATE.** Park reason in comment. |

**Fresh issues are unlabelled.** New issues filed without any `state:*` label are treated as equivalent to `state:needs-planning` — Stage 1 (plan-exec) accepts them and applies the label as part of its initial flip to `state:in-progress`. The user does not need to manually label new issues.

### Type, priority, area (unchanged)

`type:feat`, `type:fix`, `type:chore`, `type:refactor`
`priority:high`, `priority:medium`, `priority:low`
`area:bot`, `area:vocab`, `area:scheduler`, `area:llm`, `area:translator`, `area:config`, `area:db`

### Removed labels (vs the old parallel design)

- `state:ready-for-parallel-work` — replaced by `state:needs-planning` flowing directly into Stage 1.
- `state:ready-for-review` — replaced by auto-merge. There's no longer a "PR approved, awaiting human merge" state.
- `touches:bot.py` — was a parallel-execution routing hint; serial pipeline doesn't need it.

## Skills

Three skills under `.claude/skills/`. Each is auto-loaded in any Claude session opened against the repo, including headless `claude -p` runs.

| Skill | Stage | Session | Outputs |
|---|---|---|---|
| `plan-exec` | 1 | Combined plan + implement | Branch + commit; label flip; or clarification comment. Folds the old `plan-issue` skill's role into the implementer. |
| `clarify-issue` | sub-skill of 1 | — | Posts a focused question, flips label, stops. Already exists; minor wording. |
| `test-writer` | 2 | Fresh | Tests under `tests/`, pushed branch, opened PR; or rework comment. |
| `review-pr` | 3 | Fresh | Auto-merge, request-changes review, or block. |

Deleted: `plan-issue` — its decomposition role is gone (Stage 1 handles whatever the issue describes). If an issue is too big for one PR, Stage 1 raises a clarification asking the user to split.

## Reviewer's safety checks

Auto-merge means the reviewer is the last line of defense. The `review-pr` skill prompt explicitly checks the diff for:

- **Secrets and credentials.** API keys, tokens, passwords, OAuth client secrets, hard-coded `.env` values. Any hit → `state:blocked`.
- **CI/workflow tampering.** `.github/workflows/**` changes that add network egress, disable checks, or modify permissions. Any suspicious change → `state:blocked`.
- **Schema and data-loss risk.** `db.py` migrations that drop columns, change PKs, or break the FSRS columns. Any destructive migration → `state:blocked`.
- **Dependency additions.** New entries in `requirements.txt` — verify the package exists, is the right name (no typo-squatting), and pinned to a known version. Suspect addition → `state:blocked`.
- **Service / install scripts.** Changes to `gemma-rpi-agent.service` or `install-service.sh` — flag for human review (`state:blocked`).
- **Scope drift.** PR diff is materially larger than the issue body suggests. → `state:needs-rework`.
- **Test coverage.** New code paths without corresponding tests. → `state:needs-rework`.

There is no path-based auto-block list; the reviewer prompt does the judgment.

## Orchestrator

A long-running daemon. Polls GitHub every 60 seconds, dispatches one agent per tick.

> Detailed design space — every option per axis, recommendations, open questions: **`orchestrator-plan.md`**.

### Polling logic

```
every 60s:
  if lock-file exists and process is alive: skip tick
  acquire lock
  for each label in [needs-planning, needs-rework, tests-pending, in-review]:
    issues = gh issue list --label state:<label> --author valdisd96 --json number,labels,author
    pick the highest-priority one (priority:high > medium > low; ties = oldest)
    dispatch:
      needs-planning, needs-rework → scripts/agent-plan-exec.sh <#>
      tests-pending                → scripts/agent-test-write.sh <#>
      in-review                    → scripts/agent-review.sh <#>
    break (one issue per tick)
  release lock
```

### Cycle counter

Each rework increments a counter stored in a hidden HTML comment on the issue (`<!-- cycle:N -->`). After **5 round-trips** the orchestrator flips the issue to `state:blocked` with a comment listing the prior PR URLs. Prevents Stage1↔Stage3 ping-pong.

### Trust gate

Only issues whose `author.login == valdisd96` are processed. Issues from external users stay at `state:needs-planning` indefinitely until the user manually re-labels them as a trusted issue (re-file under own account, in practice).

### Concurrency

Single `flock`-based lock file at `/tmp/gemma-orchestrator.lock`. One issue end-to-end at a time across the whole pipeline. If a stage is running when the next tick fires, the tick is a no-op.

### Model

All agents run on **Opus 4.7** (`claude-opus-4-7`), passed via `claude -p --model claude-opus-4-7 ...`. No mixed-model strategy in v1; revisit if usage limits bite.

### Auth

- `gh` PAT on the orchestrator host with `repo` scope. Lives in `~/.config/gh/hosts.yml`.
- `claude` Pro/Max OAuth on the orchestrator host. One-time browser login on first install.

## Scripts

```
scripts/
  agent-plan-exec.sh    <issue#>     — Stage 1, manual or orchestrated
  agent-test-write.sh   <issue#>     — Stage 2
  agent-review.sh       <issue#>     — Stage 3 (resolves PR from issue)
  orchestrator.sh                    — long-running poller
  setup-labels.sh                    — provisions the label set (idempotent)
```

Each agent script:
1. Validates the issue/PR is in the expected state.
2. Builds a prompt from the issue body + comments (or PR diff).
3. Spawns `claude -p --model claude-opus-4-7 "<prompt>"` with the relevant skill name in the prompt.
4. Logs stdout/stderr to `logs/agents/<stage>-<issue#>-<timestamp>.log`.
5. Exits non-zero if the agent didn't flip the label as expected — the orchestrator logs and moves on.

## Deployment (VPS)

The orchestrator runs on a VPS (separate from the Pi running the live bot, to avoid resource contention with llama.cpp). Required:

- `git`, `gh`, `python3.11+`, `claude` CLI installed.
- Repo cloned, `.venv` activated.
- `gh auth login` and `claude login` completed.
- Systemd unit installed:
  ```
  [Unit]   Description=gemma-rpi-agent orchestrator
  [Service] Type=simple
            ExecStart=/srv/gemma-rpi-agent/scripts/orchestrator.sh
            Restart=always
            User=gemma
            WorkingDirectory=/srv/gemma-rpi-agent
  [Install] WantedBy=multi-user.target
  ```
- Logs in `logs/orchestrator.log` (rotated) and `logs/agents/*`.

## Rollout plan

| Phase | Tickets | What you do |
|---|---|---|
| 1 — manual | label cleanup, three skills, three agent scripts | Trigger each script by hand. Watch what the agents produce. Iterate the skill prompts until output is reliable. |
| 2 — autonomous | orchestrator + systemd + cycle counter + trust gate | Install on VPS. Watch it run. Stop and edit prompts if it ships bad PRs. |
| 3 — smoke tests | (later) | Add post-merge smoke tests on the bot. Use failures to file bug issues that re-enter the pipeline. |
| 4 — self-improving | (later) | Allow the agent chain to file its own issues based on observed bugs/drift. |

Skip Phase 1 at your own risk. Auto-merging unreliable agent output is the failure mode.

## Risks

1. **Headless Claude auth on VPS.** Pro/Max OAuth assumes a browser. First-install spike needs to confirm `claude login` works via SSH port-forwarding or that a token-based auth path exists.
2. **Hallucinated reviews.** Reviewer green-lights buggy code; bug ships. Accepted; smoke tests catch worst cases later.
3. **Cycle ping-pong.** Stage 1 fixes A, Stage 3 says "still has A" because of context drift. Cycle limit at 5 caps damage.
4. **Pro/Max session quotas.** Headless `-p` sessions count against the same usage budget. Always-Opus may hit weekly limits on a busy week. Mitigation: lower cycle limit, downgrade to Sonnet on `priority:low`. Defer until it bites.
5. **Repo state drift.** Orchestrator and a human editing the repo simultaneously can cause merge conflicts on the orchestrator's side. Single-instance lock prevents agent overlap, not human-vs-agent. Acceptable for a solo project.

## Out of scope for v1

- Adversarial second-opinion review (`/ultrareview` is the manual escape hatch).
- Multi-issue parallelism (single lock — explicit choice).
- Path-based auto-merge block list (reviewer prompt handles this judgment).
- Self-filing agent issues (Phase 4).
