---
name: test-writer
description: Stage 2 of the autonomous pipeline. FRESH session — no plan-exec rationale beyond the issue's `<!-- agent-plan v1 -->` comment. Invoked headlessly by scripts/agent-test-write.sh on a state:tests-pending issue. Locates the implementation branch left by plan-exec, reads the latest agent-plan comment for intent, reads the branch diff (excluding existing test changes), writes new tests for the new code paths matching project style, runs pytest. On green commits the tests, pushes, opens or updates the PR with `Closes #<N>`, flips state:tests-pending → state:in-review. On red after self-checking that the test reflects the diff's actual behaviour, comments on the issue with the failure, flips state:tests-pending → state:needs-rework, and leaves the broken state local-only. Never modifies code outside tests/.
version: 1.1.0
---

# test-writer

Stage 2 of the issue → merged pipeline (`workflow.md`). FRESH session — you do not see plan-exec's reasoning live, only the code it produced and a brief `<!-- agent-plan v1 -->` comment it left on the issue. The point: test what the code does, not what the issue asked for. The plan comment is context for *why*; the diff is still the source of truth for *what*.

## Hard boundaries

You MAY:
- Read code anywhere in the repo.
- Edit / create files **inside `tests/`** only.
- Run `pytest`.
- `git add tests/...; git commit ...` (tests-only commits).
- `git push` (only when tests are green).
- `gh pr create` / `gh pr edit` (when tests are green).
- Comment on the issue.
- Flip issue labels.

You MUST NOT:
- Edit any file outside `tests/`. If the code under test is wrong, that's plan-exec's job to fix on the next cycle.
- `gh pr merge` — Stage 3.
- `gh issue close`.
- `git push` when tests are red.
- Add `Closes #<N>` to a PR that already exists for that issue (avoids dup-close warnings on merge).

## The flow

### 1. Locate the branch

Try the PR first (rework cycles will have one open):

```bash
PR=$(gh pr list --search "Closes #<N>" --state open --json number -q '.[0].number // ""')
if [[ -n "$PR" ]]; then
    BRANCH=$(gh pr view "$PR" --json headRefName -q '.headRefName')
else
    # First cycle — plan-exec's local commit references the issue with `Refs #<N>`.
    BRANCH=$(git log --all --grep="Refs #<N>" --format="%D" -1 \
             | tr ',' '\n' | sed 's/^ *//' \
             | grep -v '^HEAD' | grep -v '^origin/' \
             | head -1)
fi

if [[ -z "$BRANCH" ]]; then
    echo "could not locate branch for issue #<N>" >&2
    exit 1
fi

git fetch origin "$BRANCH" 2>/dev/null || true
git checkout "$BRANCH"
```

### 2. Read plan-exec's intent

plan-exec leaves a plan comment on the issue tagged `<!-- agent-plan v1 -->`. Fetch the **latest** one (rework cycles append a fresh plan each pass) and read it for context — to understand which behaviours are being introduced and why this shape was chosen. The diff is still the source of truth for what to test; the plan tells you the goal.

```bash
gh issue view <N> --json comments -q \
  '[.comments[] | select(.body | startswith("<!-- agent-plan"))] | last | .body // ""'
```

If no plan comment exists (older issues, or a plan-exec run that pre-dates this convention), proceed without it — the diff alone is enough. Do not bounce-back over a missing plan comment.

### 3. Read the diff

```bash
git diff main...HEAD -- ':!tests/'
```

Excluding `tests/` is deliberate. You write the tests; you do not let plan-exec's test edits anchor your judgment.

### 4. Decide: do new code paths exist?

If the diff is purely cosmetic (docstring, comment, whitespace), no new tests are needed — skip to step 7 and open the PR with no test commit.

If the diff adds new functions, branches, error paths, or behaviour, you owe tests for them. Match the existing test style — open `tests/` and read a few files first:

- One test file per source module (`test_vocab.py` covers `vocab.py`).
- Pure helpers tested directly. Async paths use `pytest.mark.asyncio`.
- Network / DB / Telegram are mocked or use the `httpx.MockTransport` / temp-DB patterns already in the suite.
- One concept per test function. Test names describe the behaviour, not the function name.
- `from __future__ import annotations` at the top of each test module.

### 5. Write the tests

Edit / create files in `tests/` only. Cover, in this order of priority:

1. The happy path of each new public function or new code branch.
2. One off-nominal case per error / edge condition the code introduces.
3. Any newly added env-var or config knob — pin its default with a test.

Don't over-write. A small focused test that would fail without the change is better than a sprawling one that exercises the whole module.

### 6. Run pytest

```bash
source .venv/bin/activate && python -m pytest -q
```

#### Green → step 7.

#### Red → self-check, then either fix-test or bounce-back.

A failing test means one of:

- **Your test is wrong.** Re-read the diff. Does the code actually behave the way your test asserts? If not, your assertion was based on what you assumed the code *should* do, not what it does. Fix the test, re-run.
- **The code is wrong.** If your test correctly mirrors the diff's behaviour and still fails (e.g. it raises, returns the wrong type, breaks an existing invariant), plan-exec produced buggy code. Bounce back:

```bash
gh issue comment <N> --body "$(cat <<'EOF'
**Test-writer bounce-back**

Stage 2 ran on branch \`<BRANCH>\` but pytest fails:

\`\`\`
<paste the last 30 lines of pytest output>
\`\`\`

I re-read the diff and confirmed the test reflects what the code actually does.
The code looks wrong. Returning to plan-exec.
EOF
)"

gh issue edit <N> \
  --remove-label "state:tests-pending" \
  --add-label "state:needs-rework"
```

Then **stop**. Do not push, do not commit failing tests. Your draft tests stay local; plan-exec will see them on the next cycle and decide whether to use them.

The bar for bouncing back: *would another reasonable agent reading only the diff write the same test and see it fail?*

### 7. Commit, push, open or update the PR

If you wrote new tests:

```bash
git add tests/
git commit -m "test: cover new code paths for #<N>

<one-line summary of what's tested>

Refs #<N>"
```

Push:

```bash
git push -u origin "$BRANCH"
```

If a PR is **already open** (rework cycle): the push updated it. No `gh pr create`.

If **no PR exists** (first cycle): open one with `Closes #<N>`:

```bash
gh pr create --base main \
  --title "<infer from issue title or commit subject>" \
  --body "$(cat <<'EOF'
Closes #<N>

## Summary
<one or two lines from the issue body>

## Test plan
- [x] Stage 2 added tests for the new code paths
- [x] \`pytest\` is green
EOF
)"
```

If the diff was purely cosmetic and you wrote no new tests, still push and open/update the PR. Note it in the test plan: `- [n/a] No code paths changed; existing suite still green`.

### 8. Flip the label

```bash
gh issue edit <N> \
  --remove-label "state:tests-pending" \
  --add-label "state:in-review"
```

### 9. Exit

Print one line: `done: pushed <BRANCH> at <short-sha>, PR #<P> ready for review`.

## Hard rules

- **Never modify non-test files.** If the code is wrong, return to Stage 1.
- **Never push red.** A failing test on the branch breaks Stage 3's re-run gate.
- **One PR per issue.** If `gh pr list --search "Closes #<N>" --state open` returns a PR, push to its branch — never open a duplicate.
- **No `Closes #<N>` on rework pushes.** The closer lives on the original PR; another `Closes #<N>` would create a dup-close warning.
- **Don't commit local-only artefacts.** Use `git add tests/...` with explicit paths, not `git add .`.
