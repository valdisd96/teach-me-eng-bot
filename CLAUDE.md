# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Telegram English-tutor bot. Users add vocabulary with `/add`; once a day the bot sends a **cloze-story session** — one short tone-flavoured story in simple learner-level English (CEFR A2–B1) built from the day's FSRS-selected words, with each word replaced by a numbered blank and a shuffled word bank below. The user types the missing word for each blank; every answer applies the FSRS rating (correct → Good, miss → Again). Plain chat messages get a one-shot reply (typing indicator while composing) from any OpenAI-compatible chat-completions endpoint with the chat's vocab injected as soft hints.

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
| `/help` | Shows a getting-started intro, an "Answering the daily story" section (all accepted answer forms with examples — bare word, `4 dog` out-of-order, comma-separated batches, `skip` — plus grading tolerance and ungraded stray text), and lists all commands with descriptions. |
| `/clear` | Resets the chat history (LLM memory) for this chat. Vocab and settings untouched. |
| `/add <word or phrase>` | Add a word to this chat's vocab. Normalized to lowercased+stripped form. New single words are spell-checked against a plain dictionary (`spelling.suggest`, pyspellchecker — no LLM per issue #101): a misspelling gets a `did you mean "…"?` reply with two inline buttons (add the correction / add as typed, both via the `av:` pending-vocab flow) instead of an immediate insert; phrases, non-alphabetic tokens, already-present words, and checker failures skip the check. Labels are managed manually via `/label` / `/unlabel` or in bulk by uploading a labelled CSV through `/import`. |
| `/remove <word or phrase>` | Remove by exact (normalized) match. |
| `/list [--any] [<spec>...]` | List vocab words (least-mentioned first). With no args, shows every word. Every row appends its labels as `— label1, label2` (alphabetical) when the word has any. With one or more spec tokens (same `key:value` / bare-string syntax as `/label`), shows only words tagged with **all** listed labels (AND); a leading `--any` (case-insensitive, position 0 only) flips the filter to **OR** — `/list --any pos:noun type:medicine` returns words tagged with either. Output spills across multiple Telegram messages so 100+ vocab lists are never truncated. Empty result → `no words match those labels`. Malformed spec → `⚠️ malformed label spec: …` and no DB query. `--any` with no following tokens → `⚠️ malformed label spec: --any`. |
| `/import` | Bulk-import vocab from a CSV file. Bot prompts for an upload (5-min window). One word/phrase per row in the first column; an optional `text` header is supported. Words are normalized + deduped against existing vocab; merge semantics — existing words are preserved, in-file duplicates are skipped. Reply summarizes `added` / `skipped (duplicate)` / `skipped (empty)`. Capped at 5000 rows / 1 MB. FSRS state is **not** imported. |
| `/export` | Send this chat's vocab back as a CSV attachment (`vocab-YYYY-MM-DD.csv`), one word per row, alphabetically sorted, with a `text` header. FSRS state is **not** exported. |
| `/resetvocab` | Wipes the chat's vocabulary (with a confirm button). |
| `/tr <text>` | Google-translate the args (or, if sent as a reply, the replied message) into the chat's configured target language. If the input is written in the target's script (e.g. Cyrillic for `ru`), reverse-translate it to English instead. The reply carries an `➕ Add to vocab` button that, when tapped, adds the English side of the pair (the source when forward-translating, the translation when reverse-translating) to this chat's vocab — the button label flips to `added to vocab ✅` or `already in vocab`. Phrases longer than 5 words skip the button and show the inline note `not added (N words)`. Does **not** invoke the LLM. Reverse detection only fires for non-Latin targets. |
| `/games [<spec>...]` | Pick a game from an inline menu. Bare `/games` shows up to four buttons: **Word → Translation** and **Translation → Word** (vocab quiz — prompt = English word / translation, 4 inline buttons per round, 1 correct + 3 distractors drawn from the same game's pool), **Irregular verbs** (static-table type-the-trio: prompt = base form, free-text reply with past simple + past participle, accepted as `went / gone`, comma- or whitespace-separated, case-insensitive; wrong answers reply with the canonical correct trio), and **Typed drill** (free-text translation drill over the chat's in-progress, focus-scoped non-`remembered` words — random direction per round EN→target or target→EN, strict case-insensitive grading, up to `typed_drill.MAX_ROUNDS` (10) rounds — plus up to `MAX_SALT` (2) salted `remembered`-not-`mastered` words per game as mastery checks: salted rounds are graded by the tolerant LLM yes/no judge (10 s timeout; unavailable judge → scored strict-wrong but recorded `source="game"` so it can't demote), recorded `source="repeat"` so a miss forget-flips the word (detach `remembered`, attach `focus:hard`) and two consecutive corrects on the same word auto-attach `mastered`, removing it from the salt pool; the end-of-game summary adds a `Wrong: …` line naming missed words). The two vocab buttons are only included when the chat has at least 4 translatable vocab rows; the irregular-verbs and Typed drill buttons are always included (the Typed drill button replies `not enough words yet — add more or widen /focus (need at least 5)` when fewer than 5 focus-scoped non-remembered translatable rows exist; the salt never counts toward that minimum). With one or more spec tokens (same `key:value` / bare-string syntax as `/label`), the pool is restricted to words tagged with **all** listed labels (AND) and the picker offers only the two vocab buttons (the Typed drill uses its own focus-scoped pool); the chosen filter survives the picker tap. Each vocab/irregular-verbs game runs `min(10, pool_size)` rounds. The final message is `🎯 You scored X/N`. Game state is in-memory only (a restart abandons in-flight games); only one game per chat at a time — `/games` while anything is running replies the matching in-progress message (`you have a game in progress`, `you have an irregular-verbs game in progress`, `you have a typed drill in progress`, or the unfinished-daily-story message; all gates share `bot._in_progress_reply`). Taps on `gm:` buttons from menus sent before the current picker layout reply `that game menu is outdated — send /games again`. With a label spec that yields fewer than 4 playable rows the bot replies ``no words match those labels — try fewer filters or `/label` more words``. Malformed spec → `⚠️ malformed label spec: …` and no picker. `/games cancel` (case-insensitive, single token) ends any in-flight game — including an unfinished daily story session — so a new one can be started — replies `🛑 game cancelled` if a game was running, otherwise `no game in progress`; also clears any leftover label-spec stash from a `/games <spec>` whose picker was never tapped. |
| `/label <word> <spec>...` | Attach one or more labels to a vocab word. Spec tokens are bare strings (`medicine`) or `key:value` (`pos:noun`, `type:animal`); each token is stripped + lowercased, duplicates are dropped, malformed tokens (`:`, `:foo`, `foo:`, `a:b:c`, internal whitespace) abort the call with a ⚠️ error and no writes. Word lookup is case-insensitive (matches `/remove`). Idempotent — re-applying an attached label replies `already attached: …` and inserts nothing. Attaching `pos:*` to a word that already has a different `pos:*` quietly replaces it; the reply names both, e.g. `horse: replaced pos:noun → pos:verb`. |
| `/unlabel <word> <spec>...` | Detach the named labels from a word. Same spec syntax as `/label`. Tokens that aren't attached (or whose label doesn't exist) are silently skipped; reply lists the names actually removed, or `nothing to remove`. |
| `/labels` | List every label in this chat with its attached-word count, alphabetically. Empty chat → `No labels yet.` Labels with no attached words still appear with `(0)`. |
| `/focus [--any] [<spec>...]` | Sticky per-chat label spec that scopes the scheduled **daily story** and the **Typed drill** pool. `/focus pos:noun type:medicine` stores the normalised tokens with **AND** across them (a word must carry every label); a leading `--any` flips the spec to **OR** — `/focus --any type:body type:medicine` matches words tagged with body OR medicine. The flag is recognised only at position 0 (case-insensitive); elsewhere it parses as a regular bare label. `/focus clear` removes the focus; `/focus` with no args echoes the current setting verbatim including the `--any` flag (`current focus: …` or `no focus set`). The reply to a successful set includes the matching word count, or `⚠️ no words match yet` when the filter currently selects zero rows (still stored). `--any` with zero following tokens replies `⚠️ malformed label spec: --any` and writes nothing. When focus is set and the daily session tick finds zero matches, the bot logs and sends nothing (no Telegram error) — except introduction seeds, which draw new (`reps < 2`) words into the session regardless of focus. `/games` and `/list` are unaffected — they use only their own inline `<spec>` (AND only). |
| `/top` | Report learning progress within the chat's current `/focus` spec. Replies with three sections in order: **Top (N)** — focus-matching words that carry neither `remembered` nor `focus:hard`, sorted by `remembered_streak` DESC then text ASC, each row `• <word> — score <s>` where `<s>` is the streak to one decimal (0.0…3.0+); **Forgotten (N)** — focus-matching words carrying `focus:hard` (`remembered` words missed in a daily story or a salted Typed-drill mastery check), sorted by text; **Remembered (N)** — focus-matching words carrying `remembered`, sorted by text. Empty sections still appear with count `0` and a `(none)` line. `no focus set — set one with /focus first` when the focus spec is NULL. Takes no arguments; honours the stored focus spec's mode (AND or `--any` OR). Long outputs spill across messages so 100+ words never get truncated. |
| `/status` | Three sections: **System** (hardware, OS, deployed commit short SHA, load, temp, disk free), **Vocab** (word count, label count, current `/focus` spec or `none`), and **Model** (backend health line + a usage summary — `n/a` on the local llama backend, `$<spent> used / limit: <limit-or-unlimited>, rate <r> req / <interval>` from OpenRouter's `/auth/key`, or `unavailable (...)` on transport error). The deploy SHA is read from `/var/lib/teach-me-eng-bot/deploy.json` (written by the Hermes Dark Factory deploy reconciler on every deploy); shows `unknown` when the manifest is missing. |

Plain (non-slash) messages go through the **just-talk** flow: the chat history is passed to the model with the current vocab list injected into the system prompt as soft hints. Any vocab words that appear literally in the reply bump `mention_count` and update `last_used_at`.

The scheduled **daily cloze-story session** (one per day, at a random time inside the active window) selects up to `words_per_day` words, asks the LLM for one story in the chosen tone that uses every word literally exactly once (retried once — the retry prompt names the omitted words, the tone is resolved once so a retry can't switch it, and the attempt with the fewest missing words wins; words still missing are dropped from the session), and sends the story with each word replaced by a numbered `___(n)` blank plus a shuffled word bank (duplicate occurrences of a blanked word are masked with an unnumbered `___` so they can't leak the answer). The user types answers in any of these forms, combinable via comma/semicolon/newline separators: a bare word (targets the lowest unanswered blank), `4 word` (targets blank 4 explicitly, any order; `4:`/`4.`/`4)` separators also accepted), several per message (`dog, run, cat` fills the next open blanks in order; `2 dog, 5 cat` targets specific ones), and the skip tokens (`skip`, `?` — graded as a miss; `4 skip` and in-batch skips work). Resolution is atomic: every segment must be a word-bank word or a skip against a distinct open blank, otherwise the WHOLE message gets a hint reply and is **not** graded, so a stray chat message can't rate a word `Again`; answers are matched case/whitespace-insensitively with surrounding punctuation stripped ("cat." matches "cat") and an optional leading "to ". Each real answer applies the FSRS rating (correct → `Good`, miss/skip → `Again`) and `record_outcome(source="push")`. The final message shows the completed story (words bolded), the score, and translations of the missed words, plus one `💡 <word>` inline button per missed word (`exp:<word_id>` callback → `compose_explanation` sends a one-line meaning + example). The in-flight session is persisted to `push_log.session_json` after the send and after every answer and **rehydrated on bot restart**, so deploys don't strand the day's story; `/games cancel` clears it and marks the push_log row rated so it won't be rehydrated. Scheduling is restart-safe: at plan time the runner skips the day if `push_log` already holds a session sent today (chat-local date), and if the bot restarts after the rolled time passed it samples the *remaining* window instead of dropping the day (`scheduler.plan_session_time` / `scheduler.sent_today`). If a push_log send fails the row is deleted so the day can be replanned. Dispatch defers (in-memory `deferred_sessions`) while a typed game is in flight and fires when that game completes or is cancelled. The daily story and all games share one in-progress gate (`bot._in_progress_reply`) — starting a game during an unfinished story replies `you have an unfinished daily story …`, and vice versa. Legacy `✅ knew / ❌ forgot` buttons on pre-pivot messages hit a retired stub (`on_rate`) that clears the buttons without rating.

Up to `cloze.MAX_INTRO_WORDS` (2) of each session's words are **introduction seeds**: drawn from the introduction pool — words with fewer than `INTRO_GRADUATION_REPS` (2) FSRS ratings, `remembered` excluded — and **bypassing the `/focus` filter**, so a freshly `/add`-ed unlabelled word is guaranteed exposure while still fresh in memory. They are listed under a 🆕 header (with translations) above the story and take part in the blanks. Once a word has been rated twice it graduates into the regular pool and normal focus rules apply. When focus is set and it matches zero words, the session still runs if intro words exist; with no words at all the bot logs and sends nothing.

## Architecture

Code is split into focused modules (entrypoint is `bot.py`):

- **`bot.py`** — python-telegram-bot wiring. Command/callback/message handlers, scheduler bootstrap, transcript/history management (capped at `MAX_HISTORY_MESSAGES`), DB connection lifecycle, and a central `on_error` handler (logs every escaped exception and sends the chat a one-line ⚠️ instead of dead silence).
- **`llm.py`** — OpenAI-compatible HTTP client (local server or OpenRouter, selected by `LLM_BACKEND`). `chat()` one-shot (just-talk replies, story composer, drill judge — takes a `timeout` kwarg), `health()` and `usage()` for `/status`. Completion parsing is factored into a pure helper.
- **`sysinfo.py`** — pure readers for host diagnostics used by `/status` (hardware, OS, load, temp, disk free). Each reader has an injectable dependency and a safe fallback so `/status` works on hosts that don't expose the underlying files.
- **`vocab.py`** — vocabulary CRUD, literal mention scanning, FSRS rating (`rate_word`), and the weighted-random `select_session_words`. Uses `py-fsrs` with `desired_retention=0.95` and `maximum_interval=7d` so review intervals stay tight.
- **`prompts.py`** — tone-flavoured push/story templates (simple learner-level English constraints) and the just-talk system-prompt composer that appends the chat's vocab as soft hints.
- **`cloze.py`** — pure scaffolding for the daily cloze-story session: `Blank`/`Session` dataclasses, `blank_story` (replace each word's first whole-word occurrence with a numbered blank; longest-first so phrases aren't shadowed by their sub-words; reports missing words), `resolve_answers` (parses a plain-text message into `Answer(blank_index, text, is_skip)` records — bare word / `4 word` numbered / comma-separated batches / skip tokens; atomic: any unresolvable segment → None, whole message ungraded), `grade_answer` (case/whitespace-insensitive, leading "to " optional), `apply_answer(session, blank_index, correct)` (out-of-order safe; `Session.answered` index list replaces the old sequential `current_blank` field, which `session_from_json` still migrates from legacy payloads), `session_to_json`/`session_from_json` (push_log persistence for restart rehydration), and the HTML-safe formatters (`format_session_message` with 🆕 intro section + shuffled word bank, `format_blank_prompt`, `format_answer_feedback`, `format_not_answer_hint`, `format_result`). No telegram imports; `bot.py` holds the per-chat in-memory session map and routes plain-text answers.
- **`config_flow.py`** — `Settings` dataclass + `ConfigSession` state machine for `/start`; per-step validators (IANA tz, 4–10 words per daily story, HH:MM, known tone, known target language via `translator.normalize_target`); `save_settings` / `load_settings` upsert against the `chats` table.
- **`translator.py`** — thin wrapper around `deep_translator.GoogleTranslator` for `/tr`. `normalize_target` maps a name or ISO code to an ISO code (no network); `translate(text, target, source='auto')` does the Google call; `is_target_script(text, target)` decides reverse-translate intent by Unicode script; `vocab_target` picks the English side of the pair (source for forward, translation for reverse); `format_vocab_note` builds the 1-line vocab-add status shown inline when the 5-word cap is exceeded; `PendingVocab` is the short-token↔word registry backing the `➕ Add to vocab` button. Explicitly bypasses the LLM because small free-tier models translate weakly into non-English scripts.
- **`scheduler.py`** — `plan_session_time` (one uniform-random minute in the active window strictly after `now`, so a restart samples the remaining window), `sent_today` (chat-local-date check against push_log that makes replanning idempotent), `compose_session` (select the day's words + LLM story call + retry once if any word missing + blank via `cloze.blank_story`), `compose_explanation` (💡-button follow-up on missed story words), `log_push` / `mark_rated` / `delete_push` (failed sends) / `save_session_json` / `load_unfinished_session_json` (restart rehydration), and `PushRunner` wrapping `AsyncIOScheduler` with per-chat daily re-planning at 00:01 local.
- **`spelling.py`** — lazy pyspellchecker wrapper for `/add`: `suggest(word)` returns a dictionary correction for a misspelled single word, or None for phrases, non-alphabetic tokens, known words, and unknowns without a candidate. Sync; bot.py calls it via `asyncio.to_thread`.
- **`db.py`** — SQLite schema (`chats`, `words`, `push_log`) with FSRS columns on `words`; `connect()` sets WAL + `PRAGMA foreign_keys=ON`; `init_db()` applies forward-only column migrations.
- **`games.py`** — pure scaffolding for `/games`: `Round`/`Game` dataclasses, `draw_rounds(rows, *, direction="wt"|"tw", ...)` (sample `min(10, N)` correct words + 3-distractor option sets per round, both without replacement; the `direction` kwarg picks whether the prompt is the English text and options are translations, or vice versa), `apply_answer` (mutates score and current_round), `format_result`. No telegram imports; `bot.py` does the wiring and holds the per-chat in-memory map.
- **`irregular_verbs.py`** — pure scaffolding for the irregular-verbs game (reached via the `/games` picker): a static `IRREGULAR_VERBS` table of `(base, past_simple_alts, past_participle_alts)` (same for every chat), `Round`/`Game` dataclasses, `draw_rounds(...)` (sample `min(10, N)` verbs without replacement), `grade_answer(text, rd)` (split on `/`, `,`, whitespace; case-insensitive match against the alt lists), `apply_answer`, `format_result`. No telegram imports; `bot.py` starts the game from the `gm:irr` callback and intercepts plain-text replies in `handle_message` while a game is in flight.
- **`typed_drill.py`** — the "Typed drill" engine (`gm:drill`): `Round`/`Game` dataclasses (`Round.judged=True` marks a salted `remembered` mastery-check round; Game tracks score, current_round, and a `wrong` list of original English words), `draw_rounds(focus_rows, remembered_rows=(), *, n_max=MAX_ROUNDS, n_salt=MAX_SALT, min_rounds=MIN_ROUNDS, rng=...)` (filters both pools for usable translations; raises `ValueError` when the FOCUS pool is smaller than `min_rounds` — the salt never counts toward the minimum; words in both pools play as focus rounds; up to `n_salt` salted `judged=True` rounds, focus rounds fill `min(n_max − salt, pool)`; final order shuffled), `grade_answer(text, rd)` (strict: `user.strip().casefold() == expected.strip().casefold()`), `grade_answer_llm` (tolerant LLM yes/no judge with a strict fast path and a `JUDGE_TIMEOUT_S` cap; returns None when unavailable — the module's one impure function), `apply_answer(game, correct, *, source_word)`, `format_result(score, n, wrong)` ("🎯 You scored X/N" plus a `Wrong: …` line when non-empty). No telegram imports; `bot.py` holds the single `typed_drills` map, builds the focus pool via `_playable_rows(conn, chat_id, focus_names, mode=focus_mode)` and the salt pool as `remembered − mastered`, and one `handle_message` branch keyed on `rd.judged` grades salted rounds with `grade_answer_llm` + `record_outcome(source="repeat")` (drives the forget-flip / mastered machinery; judge unavailable → strict-wrong + `source="game"`) and regular rounds with strict `grade_answer` + `source="game"`.
- **`tests/`** — pytest suite covering schema constraints, vocab CRUD, mention scanning, FSRS state transitions, selection-weight math, deterministic weighted sampling, prompt composition, tz/time validators, config session transitions, session planning (restart-safety, sent-today), compose_session retry paths, push_log roundtrip, PushRunner job registration, session persistence/rehydration, and the games/story in-progress gates.

### Selection weight

Session words are drawn by weighted sampling (`random.choices`) using:

```
weight(row) = (1 + forget_prob)     # FSRS; 1 - retrievability; unrated → 1.0
            * (1 + recency_boost)    # exp(-age_days / 7)
            * (1 + rarity_boost)     # 1 / (1 + mention_count)
```

Each factor lives in `[0, 1]`, lifted to `[1, 2]` so no single signal dominates.

`vocab.select_session_words` builds the daily session's word list: up to `max_intro` weighted picks from the introduction pool (`reps < INTRO_GRADUATION_REPS`, minus `remembered`, ignoring label filters) come first, then the remaining slots are filled from the (focus-scoped) regular pool — all sampled without replacement via the same weight formula (`_sample_weighted_many`).

`remembered` words are normally excluded from selection, but graduation is not forever: a remembered word whose FSRS forgetting probability has decayed to `REMEMBERED_REVIVAL_FORGET_PROB` (0.3) or above is **re-admitted** to session selection (revival requires actual FSRS state — never-rated remembered words stay excluded). Missing a re-admitted word in a story forget-flips it (detach `remembered`, attach `focus:hard`) via `record_outcome(source="push")`; an ordinary in-progress miss only resets the streak.

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
  - `push_log(id PK, chat_id, sent_at, tg_message_id, word_ids_json, rated, session_json)` — `session_json` holds the serialized in-flight cloze session (NULL once irrelevant; ignored when `rated=1`)
- Cascade-delete: removing a chat drops its words and push_log entries.

## Logs on disk

- `logs/bot.log` — rotating file log (5 MB × 5 backups), mirrors journald output. `httpx` / `telegram` / `apscheduler` loggers are pinned to WARNING so the bot token never appears in request URLs.
- `logs/convs/<chat_id>/NNN.txt` — human-readable transcript, one file per conversation, zero-padded sequential numbering per chat. Push messages are included tagged `push`.
- `logs/` and `data/` are git-ignored.

## Key constants

- `bot.py`: `MAX_MSG_LEN = 4000` (per-message cap; long responses spill into additional messages), `MAX_HISTORY_MESSAGES = 41` (system + 40 turns; older just-talk turns are dropped).
- `llm.py`: `LLAMA_URL`, `MODEL = "gemma4"` (model id sent to the local backend; OpenRouter uses `OPENROUTER_MODEL`); `chat()` takes a `timeout` kwarg (default 180 s).
- `vocab.py`: `FSRS_RETENTION = 0.95`, `FSRS_MAX_DAYS = 7`, `RECENCY_TAU_DAYS = 7.0`, `INTRO_GRADUATION_REPS = 2` (ratings before a word leaves the introduction pool), `REMEMBERED_REVIVAL_FORGET_PROB = 0.3` (forget-prob at which a remembered word re-enters selection).
- `cloze.py`: `MAX_INTRO_WORDS = 2` (introduction seeds per daily session), `SKIP_TOKENS` (`skip`, `?`).
- `typed_drill.py`: `MIN_ROUNDS = 5`, `MAX_ROUNDS = 10`, `MAX_SALT = 2`, `JUDGE_TIMEOUT_S = 10`.
- `config_flow.py`: `MIN_WORDS = 4`, `MAX_WORDS = 10` (words per daily story, stored in the legacy `pushes_per_day` column — DB default is 4 and `init_db` lifts legacy sub-4 rows).

## Agent fabric

Day-to-day changes here are driven from issues by an external agent pipeline maintained in [`valdisd96/agent-fabric`](https://github.com/valdisd96/agent-fabric). The fabric owns the `state:*` label workflow, the `plan-exec` → `test-writer` → `review-pr` skills, and the cross-project orchestrator. The local `.claude/skills/` directory is rendered from there; see that repo's `DESIGN.md` for the architecture and `workflow.md` here for the state machine.
