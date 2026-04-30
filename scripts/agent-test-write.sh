#!/usr/bin/env bash
# agent-test-write.sh — Stage 2 runner for the autonomous pipeline.
#
# Spawns a headless Claude session with the test-writer skill loaded
# against an issue at state:tests-pending. Validates state, dispatches,
# logs to logs/agents/<N>/test-write-<ts>.log.
#
# Usage: scripts/agent-test-write.sh <issue-number>
#
# Exit codes:
#   0  — claude session ran (does not imply success; check logs and labels)
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
        echo "agent-test-write: '$cmd' is required but not in PATH" >&2
        exit 3
    fi
done

# --- state validation ---------------------------------------------------

if ! issue_json=$(gh issue view "$ISSUE" --json state,labels 2>&1); then
    echo "agent-test-write: cannot fetch issue #$ISSUE: $issue_json" >&2
    exit 2
fi

issue_state=$(echo "$issue_json" | jq -r '.state')
if [[ "$issue_state" != "OPEN" ]]; then
    echo "agent-test-write: issue #$ISSUE is $issue_state, expected OPEN" >&2
    exit 2
fi

state_label=$(echo "$issue_json" | jq -r '[.labels[].name] | map(select(startswith("state:"))) | .[0] // ""')
if [[ "$state_label" != "state:tests-pending" ]]; then
    echo "agent-test-write: issue #$ISSUE is at '$state_label', expected state:tests-pending" >&2
    exit 2
fi

# --- log paths ----------------------------------------------------------

repo_root="$(git rev-parse --show-toplevel)"
log_dir="${repo_root}/logs/agents/${ISSUE}"
mkdir -p "$log_dir"
ts=$(date +%Y%m%d-%H%M%S)
log="${log_dir}/test-write-${ts}.log"

# --- dispatch -----------------------------------------------------------

prompt="Run the test-writer skill for issue #${ISSUE}. The implementation branch was created by plan-exec and may exist locally and/or as an open PR. Locate it before reading the diff."

echo "[test-write] dispatching for issue #${ISSUE}"
echo "[test-write] log: ${log}"

# --output-format stream-json --verbose: emit a JSONL audit trail of every
# tool call, message, and result event. Raw JSONL goes to "$log"; the final
# assistant message is extracted via jq for the human watching the terminal.
# Pretty-print a saved log with: jq . "$log"
# --no-session-persistence: every Stage 2 run is a fresh, blind-of-Stage-1 session.
# --permission-mode bypassPermissions: required for headless — no human to approve prompts.
# IS_SANDBOX=1: claude refuses --dangerously-skip-permissions / bypassPermissions
# under root by default; the env var opts in for sandboxed VPS / container hosts.
IS_SANDBOX=1 claude -p \
    --output-format stream-json --verbose \
    --model claude-opus-4-7 \
    --no-session-persistence \
    --permission-mode bypassPermissions \
    "$prompt" 2>&1 \
    | tee "$log" \
    | jq -rR 'fromjson? | select(.type=="result") | .result // empty'

# --- post-run summary ---------------------------------------------------

echo
echo "[test-write] final issue labels:"
gh issue view "$ISSUE" --json labels -q '.labels[].name' | grep '^state:' || echo "  (no state label)"
