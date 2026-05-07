"""LLM-driven label suggestions for a freshly-added vocab word.

Wired from `bot.py::cmd_add`: after a new word lands, this module asks the
configured LLM to (a) classify the word's part-of-speech and (b) pick up to a
few labels from the chat's existing label vocabulary that semantically fit the
word. Suggestions are returned as a list of normalised label names with the
`pos:*` token prepended; `bot.py` then renders them as inline buttons that
attach the label on tap.

Failure modes are absorbed silently: any exception from the LLM call or any
malformed JSON yields an empty list, so the user-facing `/add` confirmation is
never affected by a flaky model.

Public surface:
  * `suggest_labels(word, existing_labels, llm_chat, *, max_suggestions=5)` —
    async; returns ordered list of label names. `[]` on any failure.
  * `parse_llm_response(raw, existing_labels)` — pure JSON parser; isolated so
    it can be unit-tested without an LLM.
  * `PendingLabel` — token↔(chat_id, word_id, label_name) registry, mirrors
    `translator.PendingVocab` so callback_data stays under Telegram's 64-byte
    cap and survives unicode label names.
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable


log = logging.getLogger(__name__)

POS_PREFIX = "pos:"


def _build_messages(word: str, existing_labels: list[str]) -> list[dict]:
    """Compose the chat-completion messages for the suggestion request."""
    label_list = ", ".join(existing_labels) if existing_labels else "(none yet)"
    system = (
        "You tag English vocabulary entries. Output strict JSON only — no "
        "prose, no markdown, no commentary."
    )
    user = (
        f"Word: {word}\n"
        f"Existing labels in this learner's vocab: {label_list}\n\n"
        "Return JSON of the form:\n"
        '{"pos": "<part-of-speech>", "labels": [<up to 4 existing labels that fit>]}\n\n'
        "Rules:\n"
        '- "pos" is a single lowercase token like "noun", "verb", "adjective", '
        '"adverb", "pronoun", "preposition", "conjunction", "interjection". '
        'Use "" when you cannot decide.\n'
        '- "labels" is a JSON array (possibly empty) of strings drawn ONLY '
        "from the existing-labels list above. Pick only labels that "
        "semantically fit the word. Prefer fewer accurate labels over "
        "guessing.\n"
        "Return JSON only."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _extract_json(raw: str) -> str | None:
    """Slice the first balanced JSON object out of `raw`, or return None.

    LLMs sometimes wrap their JSON in prose or fences; this nudge keeps
    parsing tolerant without going full LLM-output-recovery.
    """
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{"):
        return raw
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return raw[start : end + 1]


def parse_llm_response(raw: str, existing_labels: list[str]) -> list[str]:
    """Extract a normalised, ordered, de-duped label list from an LLM reply.

    - The `pos` field becomes `pos:<value>` and is prepended (when valid).
    - Each entry in `labels` is lowercased + stripped; only those whose
      normalised form appears in `existing_labels` survive.
    - The POS suggestion is allowed to be a label that doesn't yet exist in
      the chat's vocab — it gets created on attach.
    - Order: POS first, then labels in the order the LLM returned them.
    - Duplicates removed (first occurrence wins).
    """
    snippet = _extract_json(raw)
    if snippet is None:
        return []
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    out: list[str] = []
    seen: set[str] = set()

    pos = data.get("pos")
    if isinstance(pos, str):
        pos_tok = pos.strip().lower()
        if pos_tok.startswith(POS_PREFIX):
            pos_tok = pos_tok[len(POS_PREFIX) :]
        if pos_tok and ":" not in pos_tok and not any(c.isspace() for c in pos_tok):
            name = f"{POS_PREFIX}{pos_tok}"
            out.append(name)
            seen.add(name)

    existing_set = {e.strip().lower() for e in existing_labels}
    labels = data.get("labels")
    if isinstance(labels, list):
        for item in labels:
            if not isinstance(item, str):
                continue
            tok = item.strip().lower()
            if not tok or any(c.isspace() for c in tok):
                continue
            if tok not in existing_set:
                continue
            if tok in seen:
                continue
            out.append(tok)
            seen.add(tok)
    return out


async def suggest_labels(
    word: str,
    existing_labels: list[str],
    llm_chat: Callable[..., Awaitable[str]],
    *,
    max_suggestions: int = 5,
) -> list[str]:
    """Ask the LLM for up to `max_suggestions` labels for `word`.

    Returns `[]` on any failure (no model, raised exception, malformed reply,
    nothing semantically fits) — callers should treat absence as "no follow-up".
    """
    word = word.strip()
    if not word or max_suggestions <= 0:
        return []
    messages = _build_messages(word, existing_labels)
    try:
        # disable_reasoning: a small max_tokens budget plus the free-tier
        # auto-router can land on a reasoning-heavy model that spends the whole
        # budget on its `message.reasoning` trace and returns `content: null`.
        # Suppressing reasoning keeps the JSON answer in `content`.
        raw = await llm_chat(
            messages, max_tokens=128, temperature=0.2, disable_reasoning=True
        )
    except Exception as exc:  # noqa: BLE001 — never let a flaky LLM break /add
        log.warning("suggest_labels: LLM call failed for %r: %s", word, exc)
        return []
    return parse_llm_response(raw, existing_labels)[:max_suggestions]


class PendingLabel:
    """Short-token → (chat_id, word_id, label_name) registry for suggestion buttons.

    Same shape as `translator.PendingVocab`: in-memory, bounded LRU eviction,
    integer tokens that fit comfortably in Telegram's 64-byte callback_data
    limit even when label names contain unicode. A bot restart orphans
    outstanding tokens — taps surface as `expired` on the user side, which is
    fine.
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._store: dict[int, tuple[int, int, str]] = {}
        self._seq = 0
        self._max = max_size

    def register(self, chat_id: int, word_id: int, label_name: str) -> int:
        self._seq += 1
        token = self._seq
        self._store[token] = (chat_id, word_id, label_name)
        while len(self._store) > self._max:
            # dicts preserve insertion order — first key is oldest.
            self._store.pop(next(iter(self._store)))
        return token

    def pop(self, token: int) -> tuple[int, int, str] | None:
        return self._store.pop(token, None)

    def __len__(self) -> int:
        return len(self._store)
