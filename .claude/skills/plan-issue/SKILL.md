---
name: plan-issue
description: Use this skill when the user asks to plan a GitHub issue ("plan #N", "plan issue 42", "decompose this issue", "break this issue down"). Reads the issue body, decides whether it's a single unit of work or needs splitting, and applies the parallel-agent labels. Always intermediates between the user's raw request and an agent picking it up — even an issue the user wrote themselves goes through here.
version: 1.0.0
---

# plan-issue

Turn a raw GitHub issue into something an agent can pick up unsupervised. The output of this skill is **labels and (sometimes) child issues**, not a code plan.

## When to invoke

User says one of:
- "plan #42" / "plan issue 42"
- "decompose this issue"
- "break #42 down"
- A raw issue was just created and the user hands you the number

**Never invoke yourself.** The user must trigger it. If you spot an unplanned issue mid-conversation, mention it; don't auto-plan.

## The flow

### 1. Read the issue

```bash
gh issue view <N> --json number,title,body,labels,author,createdAt
```

Read the title and body. Check existing labels — if `state:ready-for-parallel-work` is already set, ask the user before re-planning (you may stomp on someone's prior decision).

### 2. Decide: one unit, multiple units, or ambiguous?

Ask yourself the **scope test**: "Could one agent take this, follow `dev-flow`, and end up with one focused PR ≤ ~300 LOC, in one sitting?"

- **Yes →** one unit. Go to step 3.
- **No, but the body is clear about all the moving parts →** multiple units. Go to step 4.
- **The body is genuinely ambiguous about what should happen →** ask 1–2 specific clarifying questions inline (in the conversation, **not** as an issue comment). Wait for the user. Don't decompose on a guess.

**Anti-decomposition rule.** Prefer one issue when split-cost (extra PRs to review, conflict surface, coordination) > benefit (parallelism gained). Two small follow-ons with shared context are usually one issue.

### 3. Single unit — apply labels

Set:
- `state:ready-for-parallel-work` (and remove `state:needs-planning` if present)
- `type:*` — exactly one of `feat` / `fix` / `chore` / `refactor`. Match the change kind, not the body's wording. Use the same prefix the agent's branch will end up with under `dev-flow`.
- `priority:*` — exactly one. Default `priority:medium`. Use `:high` only if the body signals urgency (incident, blocking other work, deadline). Use `:low` for nice-to-haves.
- `area:*` — zero or more, naming the modules clearly affected (`bot`, `vocab`, `scheduler`, `llm`, `translator`, `config`, `db`).
- `touches:bot.py` — set this if the change is likely to edit `bot.py` (handler wiring, command registration). The spawner reads this to avoid scheduling two such issues concurrently. **When in doubt, set it** — false positives just slow throughput; false negatives cause merge conflicts.

```bash
gh issue edit <N> \
  --remove-label "state:needs-planning" \
  --add-label "state:ready-for-parallel-work,type:feat,priority:medium,area:vocab"
```

Tell the user what you set and why, in one or two sentences. Done.

### 4. Multiple units — fan out child issues

For each child unit, create a separate issue:

```bash
gh issue create \
  --title "<child title>" \
  --body "Part of #<parent>. <one-paragraph scope.>" \
  --label "state:needs-planning"
```

Don't pre-label the children with `state:ready-for-parallel-work` — they each need their own planning pass (the user may want to re-scope before they're picked up). Leave them at `state:needs-planning`.

Then update the parent:
1. Edit the parent body to add a checklist linking each child:
   ```markdown
   Decomposed into:
   - [ ] #<a>
   - [ ] #<b>
   - [ ] #<c>
   ```
2. Set parent labels: remove `state:needs-planning`, add `state:blocked`.
3. Comment on the parent: `Decomposed by plan-issue into #<a>, #<b>, #<c>. Parent stays open until all children land.`

```bash
gh issue edit <parent> \
  --remove-label "state:needs-planning" \
  --add-label "state:blocked"
gh issue comment <parent> --body "Decomposed into #<a>, #<b>, #<c>."
```

Tell the user the issue numbers you created. Suggest they trigger `plan-issue` on each child when they're ready.

## Hard rules

- **Never** apply `state:in-progress` — that's the spawn script's job when an agent claims the issue.
- **Never** apply `state:ready-for-review` or `state:needs-rework` — those are the `review-pr` skill's job.
- **Never** create a PR or branch from this skill. You only edit issue labels and create child issues.
- **One `type:*`, one `priority:*`, one `state:*`.** Multiple area labels are fine.
- If the user disagrees with your labelling, treat their correction as authoritative and update — don't argue.

## Examples

**Issue body:** "When `/translate` fails because of a network blip, the bot replies with a stack trace. Should show a friendly error."

→ One unit. `state:ready-for-parallel-work`, `type:fix`, `priority:medium`, `area:translator`. No `touches:bot.py` (the fix lives in `translator.py`).

**Issue body:** "Replace the SQLite layer with Postgres so we can deploy to multiple Pis. Need migration script, connection pool, env vars, update README."

→ Multiple units. Children: schema-on-postgres, migration tool, connection-pool refactor, docs. Parent gets `state:blocked`.

**Issue body:** "Make the bot better."

→ Ambiguous. Ask: "Better in what direction — speed, accuracy, more commands, smaller resource footprint? And what's the trigger — is something currently bad?" Don't decompose.
