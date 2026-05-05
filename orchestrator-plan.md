> **Status:** Superseded by [`agent-fabric/DESIGN.md`](https://github.com/valdisd96/agent-fabric/blob/main/DESIGN.md). Kept here for reference until `agent-fabric` Phase 1 ships, then will be removed.

# Orchestrator Design Plan

Detailed design space for the orchestrator daemon (Ticket 7). The orchestrator is the glue that turns the three manual stage scripts (`agent-plan-exec.sh`, `agent-test-write.sh`, `agent-review.sh`) into an autonomous pipeline driven by GitHub issue labels.

This document lays out every option per design axis, with a recommended choice. Decisions get locked into `workflow.md` once approved.

## What the orchestrator must do

A long-lived process that:

1. Watches GitHub issue labels.
2. When an issue is at `state:needs-planning`, `state:needs-rework`, `state:tests-pending`, or `state:in-review`, dispatches the matching `agent-*.sh` script.
3. Enforces invariants: cycle limit (5), trust gate (only `valdisd96`), single-flight execution.
4. Logs everything; survives crashes; supports clean stop.

It does **not** add new behaviour — every stage's logic already lives in its script. The orchestrator is glue.

---

## Decision 1 — Trigger mechanism

How does the orchestrator know there's work?

| | Mechanism | Pros | Cons | Setup cost |
|---|---|---|---|---|
| **A1** | **Polling** every N seconds: `gh issue list --label state:X` | Simplest. Works behind NAT. No extra infra. | Latency = poll interval. Wastes a few API calls/min when idle. | Zero. |
| **A2** | **GitHub webhook** → small HTTP listener | Event-driven, near-zero latency. | Needs public URL (ngrok/tunnel) on Mac/Pi. Webhook secret management. Listener process to maintain. | High (tunnel + listener + secret). |
| **A3** | **Anthropic `schedule` skill** (cron-based remote agent) | Anthropic-hosted. No local daemon. | Less control. Whether it can call `gh` and `claude -p` from a routine is unverified. | Medium, but might not work. |
| **A4** | **Manual `./scripts/orchestrator.sh tick`** — one tick per invocation | Trivial. You decide when it runs. | Defeats the "set and forget" goal. Better as a debug mode of A1. | Zero. |

**Recommendation: A1 (polling, 60s default).** Latency is fine for software dev. Webhooks (A2) are a Phase 3 concern if 60s feels slow.

---

## Decision 2 — Process model

| | Model | Pros | Cons | Cleanup on crash |
|---|---|---|---|---|
| **B1** | **systemd unit** (Pi) / **launchd plist** (Mac) | Auto-restart on crash, starts at boot, logs to journal. | One config file per OS. systemd = Linux only. launchd less familiar. | Auto-restart. |
| **B2** | **`tmux` session** the user attaches/detaches | Simple. Works identically on Mac and Pi. Easy debug — just attach. | Doesn't survive reboot unless you wrap in B1. User has to remember to start it. | Manual. |
| **B3** | **systemd timer** firing every minute (run-once) | No long-running process. Each tick is self-contained. | Cron-style logic spread across timer + service unit. Tick-state must persist across runs. | N/A — process exits each tick. |
| **B4** | **`claude /loop`** with the orchestrator as a slash command | Already in Claude Code's UX. | Only works while a Claude Code session is open. Defeats "headless". | N/A. |
| **B5** | **Bare `bash orchestrator.sh &` in a terminal** | Zero infra. | Terminal closes → daemon dies. SSH disconnect → daemon dies. | None. |

**Recommendation: B2 (tmux) for now, migrate to B1 (systemd on Pi) when stable.** Pattern: `tmux new -d -s orchestrator 'bash scripts/orchestrator.sh'`. Easy to attach (`tmux a -t orchestrator`), easy to kill, no per-OS config. The systemd unit is a 10-line file added when you're ready.

---

## Decision 3 — Where it runs

| | Host | Pros | Cons |
|---|---|---|---|
| **C1** | **Mac** (your dev box) | Already authed (claude + gh). Easy debug. | Sleeps when lid closes. SSH-only access from elsewhere. |
| **C2** | **RPi** (the one running the bot) | Always on. Already deploys the bot. | Contention with llama.cpp (CPU/RAM). Pi is ARM — verify `claude` ARM binary exists. |
| **C3** | **Both** (start on Mac during dev, move to Pi when stable) | Iterate fast on Mac, then deploy. | Two installations, two `gh auth` to maintain. |

**Recommendation: C3.** Start on Mac; once the orchestrator runs unattended for a few days without surprises, move to Pi. Don't deploy on Pi while you're still iterating prompts — every change requires a Pi restart.

---

## Decision 4 — Concurrency model

How many issues / stages can be active at once?

| | Model | Behaviour | Failure mode |
|---|---|---|---|
| **D1** | **Single-flight (`flock`)** — one stage globally at a time | Tick → if a stage is running, skip; else dispatch. | None really — the queue just grows; oldest issue waits. |
| **D2** | **Per-stage** — one plan-exec, one test-writer, one reviewer running on different issues | Three parallel sessions max. Stages of the same issue still serialised by labels. | Three Claude sessions on Max sub at once → faster quota burn. Branch operations may collide if both Stage-1 sessions modify `main`. |
| **D3** | **Per-issue, fully serial** — one issue end-to-end before next starts | Rejected earlier (we agreed on stage-level serialisation). | — |
| **D4** | **No lock** — parallel by accident | Race condition: two ticks dispatch the same issue. Both sessions clobber labels. | — |

**Recommendation: D1.** It's what we agreed; the cost (one issue at a time) is minor for solo-dev pace. D2 is a Phase 3 optimisation if throughput becomes a real bottleneck.

---

## Decision 5 — Selection algorithm

Multiple eligible issues at a given tick. Which to dispatch first?

| | Algorithm | Pros | Cons |
|---|---|---|---|
| **E1** | **Pipeline-stage priority**: `in-review` > `tests-pending` > `needs-rework` > `needs-planning`; tie-break by `priority:*` then by createdAt | Drains in-flight work first; new issues wait. Predictable. | New high-priority issues sit behind low-priority in-review work. |
| **E2** | **Pure priority**: `priority:high` > `medium` > `low`; tie by createdAt | Urgent stuff jumps the queue. | High-priority `needs-planning` blocks an existing `in-review` from finishing. Halts pipeline drainage. |
| **E3** | **Hybrid**: `state:in-review` always wins (drain first), then E2 across the rest | Compromise — finish what's almost-merged, then take new high-priority. | Slightly more complex. Probably the right one. |
| **E4** | **FIFO** by createdAt regardless | Fairest. Boring. | High-priority work waits behind months-old issues. |

**Recommendation: E3.** Drain in-flight (`in-review` first), then prioritise the rest by `priority:*`. Cycle stays short → fewer surprises.

---

## Decision 6 — State tracking (cycle counter, etc.)

Where do we record per-issue metadata that doesn't fit in labels?

| | Storage | Pros | Cons |
|---|---|---|---|
| **F1** | **Hidden HTML comment on the issue** (`<!-- cycle:3 last:2026-04-29T10:00:00Z -->`) | Self-contained on GitHub. Survives orchestrator restarts. Visible in raw markdown. | Awkward to mutate (fetch + parse + edit). One round-trip per update. |
| **F2** | **Local JSONL file** (`~/.local/state/gemma-orchestrator/issues.jsonl`) | Fast. Trivially appendable. | Lost if state dir is wiped. Doesn't survive moving from Mac to Pi without copying. |
| **F3** | **SQLite file** (`logs/orchestrator.db`) | Queryable, robust. | Overkill for ~hundreds of issues. |
| **F4** | **Pure git/gh queries** (count `Refs #<N>` commits + count `request-changes` reviews) | No new storage. Source-of-truth lives where it should. | Slower; requires multiple gh calls per issue per tick. Some events not directly countable. |

**Recommendation: F1 (HTML comment).** One round-trip when incrementing, zero round-trips when reading (already in `gh issue view --json comments`). Survives host migrations (the data is on GitHub).

---

## Decision 7 — Failure handling

When `agent-*.sh` returns non-zero, or `claude -p` itself times out:

| | Behaviour | Pros | Cons |
|---|---|---|---|
| **G1** | **Park** → flip the issue to `state:blocked`, comment with the exit code | Surfaces problems immediately. Human resolves. | Noisy if there's a transient issue (network blip). |
| **G2** | **Retry** with backoff (e.g. 1m, 5m, 15m), park after 3 retries | Tolerates flakes. | More moving parts. State for retry counts. |
| **G3** | **Skip & log**, try next tick | Simplest. | Same issue can spin forever if a real bug. |
| **G4** | **Hybrid:** retry on exit codes 2 (state) and 3 (CLI missing) up to 3x; park on others | Tolerates flakes; surfaces real problems. | Most code to write. |

**Recommendation: G2** with 3 retries (60s, 5min, 15min spacing), then park to `state:blocked` with a comment listing exit code + last log path. Most failures are transient (rate limit, network); a few are real bugs deserving human eyes.

---

## Decision 8 — Cycle limit enforcement

The 5-cycle cap: how do we count and where do we trip?

| | Approach | When it trips | Storage |
|---|---|---|---|
| **H1** | **Increment counter in HTML comment** when label flips back to `state:needs-rework`. Block at 5. | After the 5th rework. | F1 |
| **H2** | **Count `state:needs-rework` events** by parsing issue timeline (`gh issue view --json timelineItems`) | Same. | None — derived from GH. |
| **H3** | **Count `request-changes` reviews** on the linked PR | Almost the same; misses cases where Stage 2 bounced back without a review. | None. |

**Recommendation: H1.** Aligns with F1, single source of truth, robust to weird timeline events. The orchestrator increments the counter when it sees `state:needs-rework` at tick start (before dispatching plan-exec). Counts both Stage-2 bounces and Stage-3 rejections — both burn a cycle of work.

---

## Decision 9 — Pause / resume / kill

| | Mechanism | Pros | Cons |
|---|---|---|---|
| **I1** | **Touch-file gate** — orchestrator checks for `./.orchestrator-paused` at every tick; skip if present | Works without IPC. Easy to script. | Forgettable — left there indefinitely. |
| **I2** | **Special label** (`state:pipeline-paused` on a sentinel issue) | Visible on GitHub. Manageable from anywhere. | Requires an issue dedicated to the flag. |
| **I3** | **systemctl stop / kill** — just kill the process | Final. | Loses any in-flight work (mid-stage). |
| **I4** | **`./scripts/orchestrator-ctl.sh pause/resume`** — wraps I1 with logging | Cleanest UX. Trivial to write. | One more script. |

**Recommendation: I1 + I3.** Touch-file for graceful pause-during-thinking; SIGTERM for hard stop. The script traps SIGTERM, releases the flock, exits cleanly. `./scripts/orchestrator-ctl.sh` is a Phase 2 nicety.

---

## Decision 10 — Visibility / observability

Where do you watch what it's doing?

| | Channel | Pros | Cons |
|---|---|---|---|
| **J1** | **`logs/orchestrator.log`** — append-only, rotated | Standard. Easy to `tail -f`. | One more log to monitor. |
| **J2** | **stdout to tmux** — read live | Trivial. | Lost on detach unless you also pipe to a file. |
| **J3** | **Issue comments by orchestrator** when it dispatches | High-signal — shows up on the issue itself. | Noisy on the issue. |
| **J4** | **Slack / email / push notification** | Real-time. | External dependency. |
| **J5** | **`./scripts/orchestrator-status.sh`** — summary command | One-shot snapshot of "what's running, what's queued". | Another script. |

**Recommendation: J1 + J2** (log to file AND stdout — `tee`). Skip J3 (chat-noise). J4/J5 are Phase 3.

---

## Decision 11 — Clarification-needed handling

When the user answers a clarification, the issue stays at `state:clarification-needed` until someone re-labels it. Who flips it back?

| | Approach | Pros | Cons |
|---|---|---|---|
| **K1** | **User flips manually** from `state:clarification-needed` → `state:in-progress` (or `state:needs-rework`) | Explicit. The user knows when their answer is complete. | One more click. |
| **K2** | **Orchestrator polls for new comments** by `valdisd96` after the bot's clarification comment, auto-flips | Smooth. | Bot might mistake a clarification reply for a follow-up question. False positives = bad work. |
| **K3** | **Magic phrase**: user comments `/resume` and orchestrator picks that up | Explicit + automatic. | Requires the user to remember the syntax. |

**Recommendation: K1 for v1, K3 for v2.** Manual flip is the simplest correct behaviour. `/resume` magic word is a Phase 2 nicety once we trust the loop.

---

## Decision 12 — Daily throttle

Earlier we agreed: no throttle in v1. Add only if Max usage limits actually bite.

**Recommendation: no throttle.**

---

## Decision 13 — Polling interval

| | Interval | Latency | API calls/day idle | Quota concern |
|---|---|---|---|---|
| **L1** | 30s | ≤30s | ~2 880 | Each gh call is ~1 unit; GH gives 5 000/hr. Fine. |
| **L2** | 60s | ≤60s | ~1 440 | Comfortable. |
| **L3** | 5 min | ≤5 min | ~290 | Sluggish for active dev. |
| **L4** | Adaptive (1m if active, 5m if idle for 30m) | Mixed | ~600–1 500 | Fancy. |

**Recommendation: L2 (60s).** Plenty fast for issue-driven work; trivially fits in GH rate limits.

---

## Decision 14 — Dry-run / replay modes

Optional but very useful during prompt iteration.

| Mode | What it does |
|---|---|
| **`--dry-run`** | Prints what it WOULD dispatch this tick. No actual `claude -p` call. |
| **`--once`** | Run one tick, exit. (For cron mode and debugging.) |
| **`--replay <issue#> <stage>`** | Force-dispatch the named stage on the named issue, ignoring labels. |

**Recommendation: include `--dry-run` and `--once` in v1.** `--replay` is great but adds complexity; phase 2.

---

## Summary table — recommended v1 build

| Axis | Choice |
|---|---|
| Trigger | Polling, 60s |
| Process model | tmux on Mac → systemd unit on Pi later |
| Host | Mac for now, migrate to Pi when stable |
| Concurrency | Single-flight via flock |
| Selection | `in-review` first, then by priority, then createdAt |
| State storage | Hidden HTML comments on issues |
| Failure handling | 3 retries with backoff, then `state:blocked` |
| Cycle limit | Counter in HTML comment, cap at 5 |
| Pause | Touch-file `./.orchestrator-paused` |
| Visibility | `logs/orchestrator.log` + tmux stdout via `tee` |
| Clarification resume | Manual label flip |
| Daily throttle | None |
| Modes | `--once`, `--dry-run`, default loop |

## Deferred to Phase 2/3

- systemd unit (after the loop runs unattended for a few days on Mac without issues)
- Webhook-driven trigger (only if 60s feels too slow)
- `/resume` magic comment for clarifications
- Per-stage parallel concurrency (D2)
- Daily throttle
- Notification channel (Slack/email)
- `./scripts/orchestrator-ctl.sh` and `--replay`

## Open questions

1. **Failure handling — G2 (retry then park) vs G1 (just park).** G2 is more code; G1 is simpler.
2. **Selection — E3 (in-review first) vs E2 (pure priority).** E3 looks clearly right but worth confirming.
3. **State storage — F1 (HTML comment) vs F2 (local JSONL).** F1 is more durable but slower; F2 is fast but tied to host. Mac-now/Pi-later means migration matters.
4. **Cycle counter — count both Stage-2 bounces and Stage-3 rejections,** or only the latter? Both burn a cycle; recommend both.
5. **Anything in the deferred list to promote into v1?**
