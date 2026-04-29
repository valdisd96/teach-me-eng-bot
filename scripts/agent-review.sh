#!/usr/bin/env bash
# agent-review.sh — Stage 3 runner for the autonomous pipeline.
#
# Spawns a headless Claude session with the review-pr skill loaded
# against an issue at state:in-review. Resolves the PR from the issue,
# validates state, dispatches. Logs to logs/agents/review-<N>-<ts>.log.
#
# Usage: scripts/agent-review.sh <issue-number>
#
# Exit codes:
#   0  — claude session ran (does not imply outcome; check logs and labels)
#   1  — bad arguments
#   2  — issue not found, closed, or in unexpected state
#   3  — required CLI missing (claude / gh / jq)
#   4  — no open PR found for the issue (pipeline is in an inconsistent state)

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
        echo "agent-review: '$cmd' is required but not in PATH" >&2
        exit 3
    fi
done

# --- state validation ---------------------------------------------------

if ! issue_json=$(gh issue view "$ISSUE" --json state,labels 2>&1); then
    echo "agent-review: cannot fetch issue #$ISSUE: $issue_json" >&2
    exit 2
fi

issue_state=$(echo "$issue_json" | jq -r '.state')
if [[ "$issue_state" != "OPEN" ]]; then
    echo "agent-review: issue #$ISSUE is $issue_state, expected OPEN" >&2
    exit 2
fi

state_label=$(echo "$issue_json" | jq -r '[.labels[].name] | map(select(startswith("state:"))) | .[0] // ""')
if [[ "$state_label" != "state:in-review" ]]; then
    echo "agent-review: issue #$ISSUE is at '$state_label', expected state:in-review" >&2
    exit 2
fi

# --- PR resolution (early so we fail fast if the pipeline is inconsistent) ---

pr_number=$(gh pr list --search "Closes #${ISSUE}" --state open --json number -q '.[0].number // ""')
if [[ -z "$pr_number" ]]; then
    echo "agent-review: no open PR found for issue #${ISSUE} (search 'Closes #${ISSUE}')" >&2
    exit 4
fi

# --- log paths ----------------------------------------------------------

repo_root="$(git rev-parse --show-toplevel)"
log_dir="${repo_root}/logs/agents"
mkdir -p "$log_dir"
ts=$(date +%Y%m%d-%H%M%S)
log="${log_dir}/review-${ISSUE}-${ts}.log"

# --- dispatch -----------------------------------------------------------

prompt="Run the review-pr skill for issue #${ISSUE}. The PR is #${pr_number}. Decide exactly one of APPROVE / REJECT / BLOCK per the skill's checklist."

echo "[review] dispatching for issue #${ISSUE} (PR #${pr_number})"
echo "[review] log: ${log}"

# --output-format stream-json --verbose: emit a JSONL audit trail of every
# tool call, message, and result event. Raw JSONL goes to "$log"; the final
# assistant message is extracted via jq for the human watching the terminal.
# Pretty-print a saved log with: jq . "$log"
# --no-session-persistence: every Stage 3 run is a fresh, isolated session.
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
echo "[review] final issue state:"
issue_after=$(gh issue view "$ISSUE" --json state,labels 2>&1 || echo '{"state":"UNKNOWN"}')
echo "  state: $(echo "$issue_after" | jq -r '.state')"
echo "  labels: $(echo "$issue_after" | jq -r '[.labels[].name | select(startswith("state:"))] | join(", ") // "(none)"')"
echo "[review] final PR state:"
gh pr view "$pr_number" --json state,mergedAt -q '"  state: " + .state + (if .mergedAt then ", merged at " + .mergedAt else "" end)' 2>&1 || true
