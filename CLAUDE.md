# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Telegram English-tutor bot. Users add vocabulary with `/add`; once a day the bot sends a **cloze-story session** — one short tone-flavoured story in simple learner-level English (CEFR A2–B1) built from the day's FSRS-selected words, with each word replaced by a numbered blank and a shuffled word bank below. The user types the missing word for each blank; every answer applies the FSRS rating (correct → Good, miss → Again). Plain chat messages stream live replies from any OpenAI-compatible chat-completions endpoint with the chat's vocab injected as soft hints.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# For running tests: pip install -r requirements-dev.txt
cp .env.example .env   # then fill in TELEGRAM_TOKEN
```

## Running

```bash
source .venv/bin/activate
python bot.py
```

```bash
source .venv/bin/activate && python -m pytest -q    # run tests
```

The bot needs an OpenAI-compatible chat-completions endpoint reachable per `LLM_BACKEND`. Default backend is a local server on `http://127.0.0.1:8080`; production deployments use `LLM_BACKEND=openrouter`.

## Environment variables (`.env`)

| Variable | Required | Default |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — |
| `SYSTEM_PROMPT` | No | `"You are a friendly English tutor chatting casually with a learner. Use natural, everyday English. If they ask about grammar, vocabulary, or usage, explain briefly with a small example."` |
| `ALLOWED_USER_IDS` | No | empty (allow all) — comma/whitespace-separated Telegram user IDs; if set, other users are silently ignored and logged |
| `LLM_BACKEND` | No | `llama` — local OpenAI-compatible server on `http://127.0.0.1:8080`. Set to `openrouter` to route chat completions to OpenRouter instead (the production backend). |
| `OPENROUTER_API_KEY` | When `LLM_BACKEND=openrouter` | — sent as `Authorization: Bearer <key>`. Empty value with `LLM_BACKEND=openrouter` raises at the first LLM call. |
| `OPENROUTER_MODEL` | No | A free-tier OpenRouter model id (see `.env.example` for the current default). Override to swap models without code changes. |

Per-chat scheduling settings (timezone, pushes-per-day, active window, tone) are collected via the `/start` conversation flow and stored in SQLite — they are **not** environment variables.

## Bot commands

| Command | Purpose |
|---|---|
| `/start` | Walks a guided config: timezone → words per daily story (4–10) → active window (HH:MM) → tone (funny/motivational/scary/bright/mixed) → target language (for `/tr`). Overwrites previous settings; vocab is preserved. The words count is stored in the legacy `pushes_per_day` DB column. |
| `/help` | Shows a getting-started intro and lists all commands with descriptions. |
| `/clear` | Resets the chat history (LLM memory) for this chat. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. Normalized to lowercased+stripped form. Labels are managed manually via `/label` / `/unlabel` or in bulk by uploading a labelled CSV through `/import`. |
| `/remove <word or phrase>` | Remove by exact (normalized) match. |
| `/list [--any] [<spec>...]` | List vocab words (least-mentioned first). With no args, shows every word. Every row appends its labels as `— label1, label2` (alphabetical) when the word has any. With one or more spec tokens (same `key:value` / bare-string syntax as `/label`), shows only words tagged with **all** listed labels (AND); a leading `--any` (case-insensitive, position 0 only) flips the filter to **OR** — `/list --any pos:noun type:medicine` returns words tagged with either. Output spills across multiple Telegram messages so 100+ vocab lists are never truncated. Empty result → `no words match those labels`. Malformed spec → `⚠️ malformed label spec: …` and no DB query. `--any` with no following tokens → `⚠️ malformed label spec: --any`. |
| `/import` | Bulk-import vocab from a CSV file. Bot prompts for an upload (5-min window). One word/phrase per row in the first column; an optional `text` header is supported. Words are normalized + deduped against existing vocab; merge semantics — existing words are preserved, in-file duplicates are skipped. Reply summarizes `added` / `skipped (duplicate)` / `skipped (empty)`. Capped at 5000 rows / 1 MB. FSRS state is **not** imported. |
| `/export` | Send this chat's vocab back as a CSV attachment (`vocab-YYYY-MM-DD.csv`), one word per row, alphabetically sorted, with a `text` header. FSRS state is **not** exported. |
| `/resetvocab` | Wipes the chat's vocabulary (with a confirm button). |
| `/tr <text>` | Google-translate the args (or, if sent as a reply, the replied message) into the chat's configured target language. If the input is written in the target's script (e.g. Cyrillic for `ru`), reverse-translate it to English instead. The reply carries an `➕ Add to vocab` button that, when tapped, adds the English side of the pair (the source when forward-translating, the translation when reverse-translating) to this chat's vocab — the button label flips to `added to vocab ✅` or `already in vocab`. Phrases longer than 5 words skip the button and show the inline note `not added (N words)`. Does **not** invoke the LLM. Reverse detection only fires for non-Latin targets. |
| `/games [<spec>...]` | Pick a game from an inline menu. Bare `/games` shows up to five buttons: **Word → Translation** and **Translation → Word** (vocab quiz — prompt = English word / translation, 4 inline buttons per round, 1 correct + 3 distractors drawn from the same game's pool), **Irregular verbs** (static-table type-the-trio: prompt = base form, free-text reply with past simple + past participle, accepted as `went / gone`, comma- or whitespace-separated, case-insensitive; wrong answers reply with the canonical correct trio), **Repeat (typed)** (drills `remembered`-labelled words that are not yet `mastered` — 5 rounds, random direction per round EN→target or target→EN, free-text reply, case-insensitive + trimmed match; the final summary lists the words missed; two consecutive correct Repeat rounds on the same word auto-attach the `mastered` system label and remove it from this pool too), and **Focus drill (typed)** (drills the chat's in-progress non-`remembered` words restricted to the current `/focus` spec — up to 10 rounds, random direction per round, same free-text grading and end-of-game summary as Repeat; with no `/focus` set it drills all non-`remembered` translatable vocab). The two vocab buttons are only included when the chat has at least 4 translatable vocab rows; the irregular-verbs, Repeat, and Focus drill buttons are always included so they stay reachable on a fresh chat (the Repeat button shows `not enough remembered words yet — keep practising (need at least 5)` if the chat has fewer than 5 remembered words; the Focus drill button shows `not enough focus words yet — add more or widen /focus (need at least 5)` when fewer than 5 focus-scoped non-remembered translatable rows exist). With one or more spec tokens (same `key:value` / bare-string syntax as `/label`), the pool is restricted to words tagged with **all** listed labels (AND) and the picker offers only the two vocab buttons (Repeat and Focus drill use their own sticky pools); the chosen filter survives the picker tap. Each vocab/irregular-verbs/Focus-drill game runs `min(10, pool_size)` rounds; Repeat is fixed at 5 rounds. The final message is `🎯 You scored X/N` (plus a `Wrong: …` line for Repeat and Focus drill when there were misses). Game state is in-memory only (a restart abandons in-flight games); only one game per chat at a time — `/games` while a vocab game is running replies `you have a game in progress`, while an irregular-verbs game is running replies `you have an irregular-verbs game in progress`, while a Repeat game is running replies `you have a repeat game in progress`, while a Focus drill is running replies `you have a focus drill in progress`. With a label spec that yields fewer than 4 playable rows the bot replies ``no words match those labels — try fewer filters or `/label` more words``. Malformed spec → `⚠️ malformed label spec: …` and no picker. `/games cancel` (case-insensitive, single token) ends any in-flight game — including an unfinished daily story session — so a new one can be started — replies `🛑 game cancelled` if a game was running, otherwise `no game in progress`; also clears any leftover label-spec stash from a `/games <spec>` whose picker was never tapped. |
| `/label <word> <spec>...` | Attach one or more labels to a vocab word. Spec tokens are bare strings (`medicine`) or `key:value` (`pos:noun`, `type:animal`); each token is stripped + lowercased, duplicates are dropped, malformed tokens (`:`, `:foo`, `foo:`, `a:b:c`, internal whitespace) abort the call with a ⚠️ error and no writes. Word lookup is case-insensitive (matches `/remove`). Idempotent — re-applying an attached label replies `already attached: …` and inserts nothing. Attaching `pos:*` to a word that already has a different `pos:*` quietly replaces it; the reply names both, e.g. `horse: replaced pos:noun → pos:verb`. |
| `/unlabel <word> <spec>...` | Detach the named labels from a word. Same spec syntax as `/label`. Tokens that aren't attached (or whose label doesn't exist) are silently skipped; reply lists the names actually removed, or `nothing to remove`. |
| `/labels` | List every label in this chat with its attached-word count, alphabetically. Empty chat → `No labels yet.` Labels with no attached words still appear with `(0)`. |
| `/focus [--any] [<spec>...]` | Sticky per-chat label spec that scopes scheduled **pushes** and the **🎮 Play game** button under the `❌ forgot` explanation. `/focus pos:noun type:medicine` stores the normalised tokens with **AND** across them (a word must carry every label); a leading `--any` flips the spec to **OR** — `/focus --any type:body type:medicine` matches words tagged with body OR medicine. The flag is recognised only at position 0 (case-insensitive); elsewhere it parses as a regular bare label. `/focus clear` removes the focus; `/focus` with no args echoes the current setting verbatim including the `--any` flag (`current focus: …` or `no focus set`). The reply to a successful set includes the matching word count, or `⚠️ no words match yet` when the filter currently selects zero rows (still stored). `--any` with zero following tokens replies `⚠️ malformed label spec: --any` and writes nothing. When focus is set and the daily session tick finds zero matches, the bot logs and sends nothing (no Telegram error) — except introduction seeds, which draw new (`reps < 2`) words into the session regardless of focus. `/games` and `/list` are unaffected — they use only their own inline `<spec>` (AND only). |
| `/top` | Report learning progress within the chat's current `/focus` spec. Replies with three sections in order: **Top (N)** — focus-matching words that carry neither `remembered` nor `focus:hard`, sorted by `remembered_streak` DESC then text ASC, each row `• <word> — score <s>` where `<s>` is the streak to one decimal (0.0…3.0+); **Forgotten (N)** — focus-matching words carrying `focus:hard` (words bounced from a Repeat game), sorted by text; **Remembered (N)** — focus-matching words carrying `remembered`, sorted by text. Empty sections still appear with count `0` and a `(none)` line. `no focus set — set one with /focus first` when the focus spec is NULL. Takes no arguments; honours the stored focus spec's mode (AND or `--any` OR). Long outputs spill across messages so 100+ words never get truncated. |
| `/status` | Three sections: **System** (hardware, OS, deployed commit short SHA, load, temp, disk free), **Vocab** (word count, label count, current `/focus` spec or `none`), and **Model** (backend health line + a usage summary — `n/a` on the local llama backend, `$<spent> used / limit: <limit-or-unlimited>, rate <r> req / <interval>` from OpenRouter's `/auth/key`, or `unavailable (...)` on transport error). The deploy SHA is read from `/var/lib/teach-me-eng-bot/deploy.json` (written by the auto-deploy workflow); shows `unknown` when the manifest is missing. |

Plain (non-slash) messages go through the **just-talk** flow: the chat history is passed to the model with the current vocab list injected into the system prompt as soft hints. Any vocab words that appear literally in the reply bump `mention_count` and update `last_used_at`.

The scheduled **daily cloze-story session** (one per day, at a random time inside the active window) selects up to `words_per_day` words, asks the LLM for one story in the chosen tone that uses every word literally exactly once (retried once; words still missing are dropped from the session), and sends the story with each word replaced by a numbered `___(n)` blank plus a shuffled word bank. The user types the answer for each blank in order; each answer applies the FSRS rating (correct → `Good`, miss → `Again`) and `record_outcome(source="push")`. The final message shows the completed story (words bolded), the score, and translations of the missed words. Session state is in-memory only (one per chat; a restart or the next day's dispatch replaces it, `/games cancel` clears it). Legacy `✅ knew / ❌ forgot` buttons on already-sent messages still work via the `rate:` callback.

Up to `cloze.MAX_INTRO_WORDS` (2) of each session's words are **introduction seeds**: drawn from the introduction pool — words with fewer than `INTRO_GRADUATION_REPS` (2) FSRS ratings, `remembered` excluded — and **bypassing the `/focus` filter**, so a freshly `/add`-ed unlabelled word is guaranteed exposure while still fresh in memory. They are listed under a 🆕 header (with translations) above the story and take part in the blanks. Once a word has been rated twice it graduates into the regular pool and normal focus rules apply. When focus is set and it matches zero words, the session still runs if intro words exist; with no words at all the bot logs and sends nothing.

## Architecture

Code is split into focused modules (entrypoint is `bot.py`):

- **`bot.py`** — python-telegram-bot wiring. Command/callback/message handlers, scheduler bootstrap, transcript/history management, DB connection lifecycle.
- **`llm.py`** — OpenAI-compatible HTTP client (local server or OpenRouter, selected by `LLM_BACKEND`). `stream_chat()` for live edits, `chat()` one-shot for pushes, `health()` and `usage()` for `/status`. SSE/completion parsing is factored into pure helpers.
- **`sysinfo.py`** — pure readers for host diagnostics used by `/status` (hardware, OS, load, temp, disk free). Each reader has an injectable dependency and a safe fallback so `/status` works on hosts that don't expose the underlying files.
- **`vocab.py`** — vocabulary CRUD, literal mention scanning, FSRS rating (`rate_word`), and the weighted-random `select_word`. Uses `py-fsrs` with `desired_retention=0.95` and `maximum_interval=7d` so review intervals stay tight.
- **`prompts.py`** — tone-flavoured push/story templates (simple learner-level English constraints) and the just-talk system-prompt composer that appends the chat's vocab as soft hints.
- **`cloze.py`** — pure scaffolding for the daily cloze-story session: `Blank`/`Session` dataclasses, `blank_story` (replace each word's first whole-word occurrence with a numbered blank; longest-first so phrases aren't shadowed by their sub-words; reports missing words), `grade_answer` (case/whitespace-insensitive, leading "to " optional), `apply_answer`, and the HTML-safe formatters (`format_session_message` with 🆕 intro section + shuffled word bank, `format_blank_prompt`, `format_answer_feedback`, `format_result`). No telegram imports; `bot.py` holds the per-chat in-memory session map and routes plain-text answers.
- **`config_flow.py`** — `Settings` dataclass + `ConfigSession` state machine for `/start`; per-step validators (IANA tz, 6–12 pushes, HH:MM, known tone, known target language via `translator.normalize_target`); `save_settings` / `load_settings` upsert against the `chats` table.
- **`translator.py`** — thin wrapper around `deep_translator.GoogleTranslator` for `/tr`. `normalize_target` maps a name or ISO code to an ISO code (no network); `translate(text, target, source='auto')` does the Google call; `is_target_script(text, target)` decides reverse-translate intent by Unicode script; `vocab_target` picks the English side of the pair (source for forward, translation for reverse); `format_vocab_note` builds the 1-line vocab-add status shown inline when the 5-word cap is exceeded; `PendingVocab` is the short-token↔word registry backing the `➕ Add to vocab` button. Explicitly bypasses the LLM because small free-tier models translate weakly into non-English scripts.
- **`scheduler.py`** — `plan_push_times` (equal-bucket sampling with half-gap edge buffers; called with n=1 for the daily session), `compose_session` (select the day's words + LLM story call + retry once if any word missing + blank via `cloze.blank_story`), `compose_explanation` / `compose_translation` (❌-miss follow-ups), `log_push` / `mark_rated`, and `PushRunner` wrapping `AsyncIOScheduler` with per-chat daily re-planning at 00:01 local.
- **`db.py`** — SQLite schema (`chats`, `words`, `push_log`) with FSRS columns on `words`; `connect()` sets WAL + `PRAGMA foreign_keys=ON`; `init_db()` applies forward-only column migrations.
- **`games.py`** — pure scaffolding for `/games`: `Round`/`Game` dataclasses, `draw_rounds(rows, *, direction="wt"|"tw", ...)` (sample `min(10, N)` correct words + 3-distractor option sets per round, both without replacement; the `direction` kwarg picks whether the prompt is the English text and options are translations, or vice versa), `apply_answer` (mutates score and current_round), `format_result`. No telegram imports; `bot.py` does the wiring and holds the per-chat in-memory map.
- **`irregular_verbs.py`** — pure scaffolding for the irregular-verbs game (reached via the `/games` picker): a static `IRREGULAR_VERBS` table of `(base, past_simple_alts, past_participle_alts)` (same for every chat), `Round`/`Game` dataclasses, `draw_rounds(...)` (sample `min(10, N)` verbs without replacement), `grade_answer(text, rd)` (split on `/`, `,`, whitespace; case-insensitive match against the alt lists), `apply_answer`, `format_result`. No telegram imports; `bot.py` starts the game from the `gm:irr` callback and intercepts plain-text replies in `handle_message` while a game is in flight.
- **`repeat_game.py`** — pure scaffolding for the "Repeat (typed)" game (reached via the `/games` picker, callback `gm:repeat`): `Round`/`Game` dataclasses (Game tracks score, current_round, and a `wrong` list of original English words), `draw_rounds(rows, *, n_rounds=5, rng=...)` (sample 5 rows without replacement, independently pick `en2ru` or `ru2en` direction per round; raises `ValueError` when fewer than 5 translatable rows are supplied), `grade_answer(text, rd)` (`user.strip().casefold() == expected.strip().casefold()`), `apply_answer(game, correct, *, source_word)` (advances and records the missed word), `format_result(score, n, wrong)` ("🎯 You scored X/N" plus a `Wrong: …` line when the list is non-empty). No telegram imports; `bot.py` builds the pool by filtering `vocab.list_words` to `vocab.remembered_word_ids`, starts the game from the `gm:repeat` callback, and intercepts plain-text replies in `handle_message` while a game is in flight.
- **`focus_drill.py`** — pure scaffolding for the "Focus drill (typed)" game (reached via the `/games` picker, callback `gm:focus`): same `Round`/`Game` shape as `repeat_game.py` (Game tracks score, current_round, `wrong` list), `draw_rounds(rows, *, n_max=10, min_rounds=5, rng=...)` (samples `min(n_max, len(translatable_pool))` rows without replacement, independently picks `en2ru` or `ru2en` per round; raises `ValueError` when the translatable pool is smaller than `min_rounds`), `grade_answer` / `apply_answer` / `format_result` mirror Repeat's signatures. No telegram imports; `bot.py` builds the pool with `_playable_rows(conn, chat_id, focus_names, mode=focus_mode)` so it's the same focus-scoped, non-`remembered` set the rest of the bot uses, starts the game from the `gm:focus` callback, intercepts plain-text replies in `handle_message`, and records outcomes via `vocab.record_outcome(..., source="game")` (no forget-flip — words are non-remembered by construction).
- **`tests/`** — pytest suite covering schema constraints, vocab CRUD, mention scanning, FSRS state transitions, selection-weight math, deterministic weighted sampling, prompt composition, tz/time validators, config session transitions, plan_push_times determinism + min-gap, compose_push retry paths, push_log roundtrip, PushRunner job registration.

### Selection weight

`vocab.select_word` samples via `random.choices` using:

```
weight(row) = (1 + forget_prob)     # FSRS; 1 - retrievability; unrated → 1.0
            * (1 + recency_boost)    # exp(-age_days / 7)
            * (1 + rarity_boost)     # 1 / (1 + mention_count)
```

Each factor lives in `[0, 1]`, lifted to `[1, 2]` so no single signal dominates.

`vocab.select_session_words` builds the daily session's word list: up to `max_intro` weighted picks from the introduction pool (`reps < INTRO_GRADUATION_REPS`, minus `remembered`, ignoring label filters) come first, then the remaining slots are filled from the (focus-scoped) regular pool — all sampled without replacement via the same weight formula (`_sample_weighted_many`). `vocab.select_intro_word` is the single-pick variant still used elsewhere.

## Bulk labelling CLI (external agents)

`scripts/labels_cli.py` lets an external agent (the Hermes-side "label new
words" skill) tag vocabulary in bulk with a stronger model than the bot's
own backend — issue #101 removed add-time LLM label suggestions precisely
because the free model was too weak, so labelling intelligence lives
outside the bot:

- `labels_cli.py dump --chat-id N` → JSON with the chat's unlabelled words
  (plus translations) and the existing non-reserved taxonomy (counts +
  example words), via `vocab.dump_labelling_state`.
- `labels_cli.py apply --chat-id N --file mapping.json [--dry-run]` →
  attaches a `{word: [labels]}` mapping through `vocab.apply_label_mapping`:
  existing words only (unknown words are reported, never inserted), spec
  validation via `parse_label_spec`, at most one `pos:*`, reserved system
  labels rejected, `attach_label` one-POS replacement semantics, idempotent.

Run with the project venv from the repo root. Neither path is imported by
`bot.py`; deploying it needs only a `git pull` on the VPS, no restart.

## SQLite

- Path: `data/vocab.db` (git-ignored). Created on first run.
- Tables:
  - `chats(chat_id PK, tz, pushes_per_day, active_start, active_end, tone, translate_target, focus_spec, created_at)`
  - `words(id PK, chat_id FK, text, added_at, mention_count, last_used_at, stability, difficulty, state, step, due, reps, lapses, last_review, translation, remembered_streak, repeat_correct_streak, UNIQUE(chat_id, text))`
  - `push_log(id PK, chat_id, sent_at, tg_message_id, word_ids_json, rated)`
- Cascade-delete: removing a chat drops its words and push_log entries.

## Logs on disk

- `logs/bot.log` — rotating file log (5 MB × 5 backups), mirrors journald output. `httpx` / `telegram` / `apscheduler` loggers are pinned to WARNING so the bot token never appears in request URLs.
- `logs/convs/<chat_id>/NNN.txt` — human-readable transcript, one file per conversation, zero-padded sequential numbering per chat. Push messages are included tagged `push`.
- `logs/` and `data/` are git-ignored.

## Key constants

- `bot.py`: `EDIT_INTERVAL = 2.0` (seconds between stream edits), `MAX_MSG_LEN = 4000` (per-message cap; long responses spill into additional messages).
- `llm.py`: `LLAMA_URL`, `MODEL = "gemma4"` (model id sent to the local backend; OpenRouter uses `OPENROUTER_MODEL`).
- `vocab.py`: `FSRS_RETENTION = 0.95`, `FSRS_MAX_DAYS = 7`, `RECENCY_TAU_DAYS = 7.0`, `INTRO_GRADUATION_REPS = 2` (ratings before a word leaves the introduction pool).
- `cloze.py`: `MAX_INTRO_WORDS = 2` (introduction seeds per daily session).
- `config_flow.py`: `MIN_WORDS = 4`, `MAX_WORDS = 10` (words per daily story, stored in the legacy `pushes_per_day` column).
- `scheduler.py`: `MIN_GAP_MIN = 45` (minimum spacing between consecutive pushes — a no-op for the single daily session).

## Agent fabric

Day-to-day changes here are driven from issues by an external agent pipeline maintained in [`valdisd96/agent-fabric`](https://github.com/valdisd96/agent-fabric). The fabric owns the `state:*` label workflow, the `plan-exec` → `test-writer` → `review-pr` skills, and the cross-project orchestrator. The local `.claude/skills/` directory is rendered from there; see that repo's `DESIGN.md` for the architecture and `workflow.md` here for the state machine.
