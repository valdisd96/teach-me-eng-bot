#!/usr/bin/env bash
# agent-epic-decompose.sh — pre-Stage-1 runner for `type:epic` issues.
#
# Spawns a headless Claude session with the epic-decompose skill loaded
# against an epic that needs decomposition into child issues. One run =
# one round (Q&A, proposal, or file-children — see the skill's decision
# tree). Multi-round flows resume by re-running this script after the
# user flips the state label back to `state:needs-planning`.
#
# Usage: scripts/agent-epic-decompose.sh <issue-number>
#
# Exit codes:
#   0  — claude session ran (does not imply success; check logs and labels)
#   1  — bad arguments
#   2  — issue not found, closed, missing type:epic, or in unexpected state
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
        echo "agent-epic-decompose: '$cmd' is required but not in PATH" >&2
        exit 3
    fi
done

# --- state validation ---------------------------------------------------

if ! issue_json=$(gh issue view "$ISSUE" --json state,labels 2>&1); then
    echo "agent-epic-decompose: cannot fetch issue #$ISSUE: $issue_json" >&2
    exit 2
fi

issue_state=$(echo "$issue_json" | jq -r '.state')
if [[ "$issue_state" != "OPEN" ]]; then
    echo "agent-epic-decompose: issue #$ISSUE is $issue_state, expected OPEN" >&2
    exit 2
fi

# Must carry type:epic — refuse otherwise so this script can't be misused
# on regular issues (which belong to plan-exec).
has_epic=$(echo "$issue_json" | jq -r '[.labels[].name] | any(. == "type:epic")')
if [[ "$has_epic" != "true" ]]; then
    echo "agent-epic-decompose: issue #$ISSUE is not labelled 'type:epic'; use agent-plan-exec.sh instead" >&2
    exit 2
fi

state_label=$(echo "$issue_json" | jq -r '[.labels[].name] | map(select(startswith("state:"))) | .[0] // ""')
case "$state_label" in
    ""|state:needs-planning|state:awaiting-decompose-approval)
        # Empty (fresh) → treat as needs-planning. awaiting-decompose-approval
        # is the resume entry point after a user provides feedback or approval.
        ;;
    *)
        echo "agent-epic-decompose: issue #$ISSUE is at '$state_label', expected unset, state:needs-planning, or state:awaiting-decompose-approval" >&2
        exit 2
        ;;
esac

# --- log paths ----------------------------------------------------------

repo_root="$(git rev-parse --show-toplevel)"
log_dir="${repo_root}/logs/agents/${ISSUE}"
mkdir -p "$log_dir"
ts=$(date +%Y%m%d-%H%M%S)
log="${log_dir}/epic-decompose-${ts}.log"

# --- dispatch -----------------------------------------------------------

prompt="Run the epic-decompose skill for epic issue #${ISSUE}. Current state: ${state_label:-unlabelled (treat as state:needs-planning)}. Walk the decision tree on every dispatch — ask the next question, post a proposal, or (if /decompose-ok was given) file the children. One round per invocation."

echo "[epic-decompose] dispatching for issue #${ISSUE} (state=${state_label:-unlabelled})"
echo "[epic-decompose] log: ${log}"

# --output-format stream-json --verbose: emit a JSONL audit trail of every
# tool call, message, and result event. Raw JSONL goes to "$log"; the final
# assistant message is extracted via jq for the human watching the terminal.
# Pretty-print a saved log with: jq . "$log"
# --no-session-persistence: each dispatch is a fresh round.
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
echo "[epic-decompose] final issue labels:"
gh issue view "$ISSUE" --json labels -q '.labels[].name' | grep -E '^(state:|type:)' || echo "  (no state/type labels)"
