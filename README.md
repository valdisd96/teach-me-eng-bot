# teach-me-eng-bot

A Telegram bot that helps you learn English vocabulary.

You save words with `/add`. Once a day the bot sends a short tone-flavoured cloze story built from your words — you type the missing word for each blank, and every answer updates an FSRS spaced-repetition schedule per word. Plain (non-slash) messages get a chat reply from an LLM with your vocab injected as soft hints.

A [python-telegram-bot](https://python-telegram-bot.org) app talking to any OpenAI-compatible chat-completions endpoint. Per-chat vocabulary, settings, and FSRS state live in SQLite (`data/vocab.db`); APScheduler sends randomised pushes inside each chat's active window.

---

## Commands

| Command | Purpose |
|---|---|
| `/start` | Guided config: timezone → pushes/day (6–12) → active window → tone (funny / motivational / scary / bright / mixed) → translate target language. Re-running overwrites settings; vocab is preserved. |
| `/help` | Lists all commands with descriptions. |
| `/clear` | Resets the chat's LLM history. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. |
| `/remove <word or phrase>` | Remove by exact match. |
| `/list [--any] [<spec>...]` | List vocab (least-mentioned first); every row shows its labels. With one or more label tokens, restrict to words tagged with **all** of them (AND); prepend `--any` for OR. Long lists spill across multiple Telegram messages. See [Labels](#labels). |
| `/import` | Bulk-import from a CSV upload (5-min window, capped at 5000 rows / 1 MB). Columns: `text` (required), optional `translation`, optional `labels` (`;`-separated names). The `text,translation,labels` header opts in to the three-column round-trip; bare-list and `text,translation` formats remain accepted. Existing words preserved; in-file duplicates skipped; label sets are merged additively. FSRS state not imported. |
| `/export` | Sends the chat's vocab back as `vocab-YYYY-MM-DD.csv`, alphabetical, with a `text,translation,labels` header. The `labels` column lists each word's labels joined by `;`. FSRS state not exported. |
| `/resetvocab` | Wipe vocabulary (with a confirm button). |
| `/tr <text>` | Google-translates args (or replied message) into the chat's target language. Reverse-translates non-Latin input back to English. Inline `➕ Add to vocab` button on results ≤5 words. Bypasses the LLM. |
| `/games [<spec>...]` | Pick a game from an inline menu: **Word → Translation**, **Translation → Word** (vocab quizzes), **Irregular verbs** (type-the-trio against a built-in static table), or **Repeat (typed)** (5 rounds of typed answers, random direction per round, drawn only from words carrying the `remembered` system label — see [Labels](#labels)). The two vocab buttons appear only when the chat has ≥4 translatable rows; the irregular-verbs button is always available; the Repeat (typed) button needs ≥5 remembered words. With one or more label tokens, restrict the pool to words tagged with **all** of them (AND) and the picker offers only the two vocab buttons; see [Labels](#labels). Vocab quizzes run `min(10, pool_size)` rounds with 4 inline buttons each (1 correct + 3 distractors); irregular verbs runs `min(10, len(table))` rounds and grades a free-text reply (`went / gone`, also accepted comma- or whitespace-separated, case-insensitive); Repeat (typed) grades typed replies case-insensitively and a wrong answer drops `remembered` and attaches `focus:hard` on that word. Final message: `🎯 You scored X/N`. One game per chat at a time; in-memory only (a restart abandons in-flight games). |
| `/label <word> <spec>...` | Attach one or more labels to a vocab word. See [Labels](#labels). |
| `/unlabel <word> <spec>...` | Detach the named labels from a vocab word. |
| `/labels` | List every label in this chat with its attached-word count. |
| `/focus [<spec>...]` | Sticky per-chat label spec that scopes scheduled pushes and the post-`❌ forgot` 🎮 button. `/focus pos:noun` sets it; `/focus clear` removes it; `/focus` echoes the current value. |
| `/status` | Three sections — System (host diagnostics), Vocab (word + label counts, current `/focus`), Model (backend health + usage / quota when on OpenRouter). |

Plain (non-slash) messages hit the LLM with vocab injected into the system prompt as soft hints. Words that appear literally in the reply bump their `mention_count` and get freshness credit.

Scheduled pushes send 1 short snippet using 1 vocab word in the chosen tone, with `✅ knew / ❌ forgot` buttons that apply FSRS `Good` / `Again`. Every 3rd push of the day is an **introduction slot** (marked 🆕): it presents a newly added word — one rated fewer than 2 times — even when the word has no labels and a `/focus` is active, so fresh words never silently vanish into the vocab. After 2 ratings the word graduates into the regular pool.

---

## Labels

Labels are per-chat tags you attach to vocab words. They let you slice your vocabulary by topic, part of speech, or any other axis you invent — and then point `/list`, `/games`, and pushes at just that slice.

**Spec syntax.** A label token is either a bare string (`medicine`, `travel`) or a `key:value` pair (`pos:noun`, `type:animal`). Tokens are stripped + lowercased; a token with internal whitespace, an empty key/value, or more than one colon is rejected with `⚠️ malformed label spec: …` and no writes happen.

**System-managed labels.** Three label names are reserved for the bot and **cannot be attached or detached by hand** — `/label foo remembered` and `/unlabel foo focus:hard` both reject with `⚠️ … is a system label — managed automatically, can't be set by hand`.

- `remembered` — **graduation tag**. Auto-attached when a word's correct-answer streak reaches the threshold (see below). Remembered words are excluded from scheduled pushes and the multi-choice vocab quizzes, and they're the **only** pool the `/games` → Repeat (typed) mode draws from.
- `focus:hard` — **boosted-focus tag**. Auto-attached when a word is missed in the Repeat (typed) game (the same miss also strips `remembered`, so the word re-enters the regular pool). Words tagged `focus:hard` are selected for pushes and vocab quizzes with **2× weight** so they resurface faster until you get them right again.
- `mastered` — **finished tag**. Auto-attached after a word is answered correctly **2** consecutive Repeat (typed) rounds. Mastered words stay `remembered` (so pushes / vocab quizzes still skip them) and additionally drop out of the Repeat pool, so they're hidden from every learning surface. A wrong Repeat round resets the consecutive count to 0 — the next mastery climb starts after the word re-graduates to `remembered`.

**Streaks and graduation.** Every vocab word carries an internal `remembered_streak`. A correct push answer (`✅ knew`) adds **+1**; a correct game answer (either the multi-choice quizzes or Repeat typed) adds **+0.5**; any wrong answer resets the streak to **0**. When the streak reaches **3**, the word is automatically labelled `remembered` and any prior `focus:hard` tag is stripped (re-graduation cancels the boost). Missing a Repeat (typed) round is the only way to lose `remembered` — it detaches the tag and attaches `focus:hard` so the word returns to the regular pool with the 2× boost. A separate per-word `repeat_correct_streak` counts only consecutive correct Repeat rounds; reaching **2** attaches `mastered` and removes the word from the Repeat pool too.

**Adding and removing.**

- `/label <word> <spec>...` — attach one or more labels to a word. Lookup is case-insensitive (matches `/remove`). Re-applying an attached label is a no-op (`already attached: …`). Attaching `pos:*` to a word that already carries a different `pos:*` quietly replaces it — a word can only have one `pos:*` at a time.
- `/unlabel <word> <spec>...` — detach the named labels. Unknown / unattached tokens are silently skipped.
- `/labels` — list every label in the chat with its attached-word count, alphabetically.

**Filters.** Three commands accept the same spec syntax as a filter; in every case the semantics are **AND across tokens** (words must carry every named label).

- `/list [--any] <spec>...` — show only words matching the filter; AND across tokens by default, OR when prefixed with `--any`. Every row displays its labels regardless of whether a filter is active.
- `/games <spec>...` — restrict the quiz pool to matching words; the chosen filter survives the direction-picker tap. The pool still needs ≥4 translatable rows or the bot replies `no words match those labels — try fewer filters or /label more words`.
- `/focus <spec>...` — **sticky** per-chat filter that scopes scheduled pushes and the **🎮 Play game** button under the `❌ forgot` explanation. `/focus clear` removes it; `/focus` with no args echoes the current setting. `/list` and `/games` are unaffected by `/focus` — they use only their own inline `<spec>`.

**CSV round-trip.** Labels travel through `/import` and `/export` in a third column named `labels`, with names joined by `;` (semicolon — `,` is already the CSV delimiter). On import the labels for each row are validated with the same spec rules and merged additively into any existing labels for that word; multiple `pos:*` on a single row reject the row. See `/import` and `/export` above for the full column contract.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN
python bot.py
```

For tests: `pip install -r requirements-dev.txt && python -m pytest -q`.

### As a systemd service

```bash
sudo bash install-service.sh
```

Copies `teach-me-eng-bot.service` to `/etc/systemd/system/`, enables it on boot, starts it.

```bash
systemctl status teach-me-eng-bot
journalctl -u teach-me-eng-bot -f
systemctl restart teach-me-eng-bot
```

---

## Environment variables

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | friendly-tutor default in `.env.example` |
| `ALLOWED_USER_IDS` | No | empty (allow all) — comma-separated Telegram user IDs |
| `LLM_BACKEND` | No | `llama` (local OpenAI-compatible server on `127.0.0.1:8080`); set to `openrouter` for the cloud backend |
| `OPENROUTER_API_KEY` | When `LLM_BACKEND=openrouter` | — |
| `OPENROUTER_MODEL` | No | a free-tier OpenRouter model — see `.env.example` |

Per-chat scheduling (timezone, pushes/day, active window, tone, translate target) lives in SQLite, populated by `/start`. Not env vars.

---

## Architecture

Per-module breakdown lives in **[`CLAUDE.md`](CLAUDE.md)**. Code is split into `bot.py` (Telegram wiring) plus dedicated modules for `llm`, `vocab`, `prompts`, `config_flow`, `scheduler`, `translator`, `sysinfo`, `games`, and `db`.

---

## How this repo evolves itself

Most changes here land through **[agent-fabric](https://github.com/valdisd96/agent-fabric)** — a small tool that drives Claude Code through a cyclical `plan-exec → test-writer → review-pr` pipeline against this repo's GitHub issues. A new issue gets triaged, planned, implemented on a branch, given tests in a fresh session, then reviewed and merged — each stage a separate Claude run with a tight contract. The skills under `.claude/skills/` are the project-side half of that contract; agent-fabric ships the rest. In practice: file an issue, label it, and the pipeline takes it from there.
