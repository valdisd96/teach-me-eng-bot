#!/usr/bin/env bash
# agent-plan-exec.sh — Stage 1 runner for the autonomous pipeline.
#
# Spawns a headless Claude session with the plan-exec skill loaded against
# a specific GitHub issue. Validates the issue exists and is in an expected
# state, then dispatches. Logs to logs/agents/plan-exec-<N>-<ts>.log.
#
# Usage: scripts/agent-plan-exec.sh <issue-number>
#
# Exit codes:
#   0  — claude session ran (does not imply the agent succeeded; check logs and labels)
#   1  — bad arguments
#   2  — issue not found, closed, or in unexpected state
#   3  — required CLI missing (claude / gh / jq)

set -euo pipefail

# --- argument parsing ---------------------------------------------------

ISSUE="${1:-}"
if [[ -z "$ISSUE" || ! "$ISSUE" =~ ^[0-9]+$ ]]; then
    echo "usage: $0 <issue-number>" >&2
    exit 1
fi

# --- dependency checks --------------------------------------------------

for cmd in claude gh jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "agent-plan-exec: '$cmd' is required but not in PATH" >&2
        exit 3
    fi
done

# --- state validation ---------------------------------------------------

if ! issue_json=$(gh issue view "$ISSUE" --json state,labels 2>&1); then
    echo "agent-plan-exec: cannot fetch issue #$ISSUE: $issue_json" >&2
    exit 2
fi

issue_state=$(echo "$issue_json" | jq -r '.state')
if [[ "$issue_state" != "OPEN" ]]; then
    echo "agent-plan-exec: issue #$ISSUE is $issue_state, expected OPEN" >&2
    exit 2
fi

state_label=$(echo "$issue_json" | jq -r '[.labels[].name] | map(select(startswith("state:"))) | .[0] // ""')
case "$state_label" in
    state:needs-planning|state:needs-rework)
        ;;
    "")
        echo "agent-plan-exec: issue #$ISSUE has no state:* label" >&2
        exit 2
        ;;
    *)
        echo "agent-plan-exec: issue #$ISSUE is at '$state_label', expected state:needs-planning or state:needs-rework" >&2
        exit 2
        ;;
esac

# --- log paths ----------------------------------------------------------

repo_root="$(git rev-parse --show-toplevel)"
log_dir="${repo_root}/logs/agents"
mkdir -p "$log_dir"
ts=$(date +%Y%m%d-%H%M%S)
log="${log_dir}/plan-exec-${ISSUE}-${ts}.log"

# --- dispatch -----------------------------------------------------------

prompt="Run the plan-exec skill for issue #${ISSUE}. Current state label: ${state_label}."

echo "[plan-exec] dispatching for issue #${ISSUE} (state=${state_label})"
echo "[plan-exec] log: ${log}"

# --no-session-persistence: each run is a fresh, unrecoverable session.
# --permission-mode bypassPermissions: required for headless — no human to approve prompts.
claude -p \
    --model claude-opus-4-7 \
    --no-session-persistence \
    --permission-mode bypassPermissions \
    "$prompt" 2>&1 | tee "$log"

# --- post-run summary ---------------------------------------------------

echo
echo "[plan-exec] final issue labels:"
gh issue view "$ISSUE" --json labels -q '.labels[].name' | grep '^state:' || echo "  (no state label)"
