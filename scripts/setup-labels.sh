#!/usr/bin/env bash
# setup-labels.sh — provision the issue label taxonomy used by the
# autonomous pipeline (plan-exec → test-writer → reviewer).
#
# Idempotent: `gh label create --force` creates the label if missing and
# updates colour/description in place if it already exists. Re-run any
# time the schema in workflow.md changes.

set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
    echo "setup-labels: gh CLI is required (https://cli.github.com)" >&2
    exit 1
fi

create() {
    local name="$1" color="$2" desc="$3"
    gh label create "${name}" --color "${color}" --description "${desc}" --force
}

# State (blue) — workflow position; one-of, flips as the issue moves.
# See workflow.md (State labels) for the full state machine.
create "state:needs-planning"               "1d76db" "Untouched. Stage 1 (plan-exec) will pick this up"
create "state:in-progress"                  "1d76db" "Plan-exec is working on this issue"
create "state:clarification-needed"         "1d76db" "Agent posted a question; awaiting user input"
create "state:tests-pending"                "1d76db" "Branch + commit ready; awaiting test-writer"
create "state:in-review"                    "1d76db" "PR open; reviewer is evaluating"
create "state:needs-rework"                 "1d76db" "Test or review failed; back to Stage 1"
create "state:awaiting-decompose-approval"  "1d76db" "Epic-decompose posted a proposal; awaiting /decompose-ok"
create "state:tracking"                     "1d76db" "Epic — children filed; waiting for them to all close"
create "state:blocked"                      "1d76db" "Stopped — needs human attention (cycle-limit, sensitive content, etc.)"

# Type (green) — maps 1:1 to dev-flow branch prefixes (epic is the exception:
# it has no branch — agent-epic-decompose.sh splits it into typed children).
create "type:feat"     "0e8a16" "New behaviour"
create "type:fix"      "0e8a16" "Bug fix"
create "type:chore"    "0e8a16" "Tooling, deps, docs"
create "type:refactor" "0e8a16" "No behaviour change"
create "type:epic"     "0e8a16" "Big feature — agent-epic-decompose.sh splits it into typed children"

# Priority (red→amber→light) — user assigns; default medium.
create "priority:high"   "d73a4a" "Urgent / blocking"
create "priority:medium" "fbca04" "Default"
create "priority:low"    "c5def5" "Nice-to-have"

# Area (gray) — optional, navigational.
create "area:bot"        "c2c2c2" "bot.py wiring layer"
create "area:vocab"      "c2c2c2" "vocab.py"
create "area:scheduler"  "c2c2c2" "scheduler.py"
create "area:llm"        "c2c2c2" "llm.py"
create "area:translator" "c2c2c2" "translator.py"
create "area:config"     "c2c2c2" "config_flow.py"
create "area:db"         "c2c2c2" "db.py"

owner_repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "setup-labels: done. View at https://github.com/${owner_repo}/labels"
