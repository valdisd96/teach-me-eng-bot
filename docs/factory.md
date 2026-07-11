# Hermes Dark Factory contract

`teach-me-eng-bot` is registered as a managed project in Valdis's Hermes Dark Factory.

## Project identity

- Slug: `teach-me-eng-bot`
- Repository: `valdisd96/teach-me-eng-bot`
- Default branch: `main`
- Runtime host: `root@178.105.242.161`
- Runtime path: `/root/teach-me-eng-bot`
- Service: `teach-me-eng-bot.service`
- Telegram factory topic: `https://t.me/c/3902141721/632`

## Autonomy model

The project starts at factory autonomy phase 1:

- Issue intake may create planner cards for issues labelled `factory:intake`.
- Planner output should route concrete work to test-writer, implementer, reviewer, docs-keeper, and merge-babysitter tasks.
- Low-risk docs/tests/small code changes may proceed through the pipeline and merge when all deterministic gates pass.
- Protected or high-risk work is still planned, but irreversible actions require explicit human approval.

## Git-flow invariant

Every code change—including emergency hotfixes, incident recovery, self-healing fixes, one-line patches, and operator-authored changes—must use an isolated branch/worktree, tests, a pull request, independent review and required gates, merge to `main`, and deployment from the merged `main` SHA. Direct commits or pushes to `main` are never allowed; urgency may shorten the review cycle but cannot bypass the PR.

## Required gates

Before PR:

- Tests or a written reason tests are not applicable.
- Compile check for touched Python modules.
- No secret-like values in the diff.

Before merge:

- CI green, or no-CI state explicitly acknowledged.
- Independent code review approval.
- Docs handoff completed or `docs_not_needed` recorded.
- No unresolved human approval gate.

## Human approval gates

Human approval is required for changes involving:

- credentials, tokens, `.env`, or API-key handling;
- auth/session/security behavior;
- billing or provider-cost behavior;
- destructive file/data operations;
- deployment infra, GitHub Actions, systemd unit changes, or remote service changes;
- SQLite migrations with data-loss/backfill risk;
- public/breaking command behavior changes;
- large architecture changes;
- weak or absent test coverage.

## Factory surfaces

- Project config: `.hermes/factory.yaml`
- Agent onboarding: `AGENTS.md`
- Testing contract: `docs/testing.md`
- Deployment contract: `docs/deploy.md`
- Architecture contract: `docs/architecture.md`

## Visibility

Project digests and alerts go to the dedicated Telegram topic:

`telegram:-1003902141721:632` (`https://t.me/c/3902141721/632`)

Messages should be concise and use `INFO`, `ACTION NEEDED`, or `ALERT` levels.
