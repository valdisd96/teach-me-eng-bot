#!/usr/bin/env bash
# setup-labels.sh — create or update the parallel-agent label taxonomy.
#
# Idempotent: `gh label create --force` creates the label if missing and
# updates colour/description in place if it already exists. Re-run any time
# the schema in parallel-agentic-plan.md changes.
#
# See `parallel-agentic-plan.md` (Label state machine) for what each label
# means and who flips it.

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
create "state:needs-planning"          "1d76db" "Raw issue, planner hasn't touched it"
create "state:ready-for-parallel-work" "1d76db" "Scoped & unambiguous — safe for an agent to pick up"
create "state:in-progress"             "1d76db" "An agent has claimed this issue"
create "state:clarification-needed"    "1d76db" "Agent posted a question; awaiting user input"
create "state:ready-for-review"        "1d76db" "PR open and review-pr passed; awaiting manual merge"
create "state:needs-rework"            "1d76db" "review-pr found issues; back to the agent"
create "state:blocked"                 "1d76db" "Waiting on another issue (see referenced #N)"

# Type (green) — maps 1:1 to dev-flow branch prefixes.
create "type:feat"     "0e8a16" "New behaviour"
create "type:fix"      "0e8a16" "Bug fix"
create "type:chore"    "0e8a16" "Tooling, deps, docs"
create "type:refactor" "0e8a16" "No behaviour change"

# Priority (red→amber→light) — planner assigns; default medium.
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

# Hint (warm) — spawner reads this to avoid scheduling two bot.py touchers.
create "touches:bot.py"  "e99695" "Don't schedule two of these concurrently"

owner_repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "setup-labels: done. View at https://github.com/${owner_repo}/labels"
