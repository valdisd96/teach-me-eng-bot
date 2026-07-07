"""Prompt templates for scheduled pushes and just-talk replies.

Pushes and chat replies share one style rule: answers should be at most 1–2
short paragraphs. Push prompts additionally require the target vocab word to
appear literally (used by the scheduler's retry-once check) and demand simple
learner-level English so the word's meaning is guessable from the context.
"""

from __future__ import annotations

import random

TONES: tuple[str, ...] = ("funny", "motivational", "scary", "bright")

_TONE_INSTRUCTIONS: dict[str, str] = {
    "funny": "Write a playful everyday moment with one light joke.",
    "motivational": "Write a warm, encouraging line, like a friendly coach.",
    "scary": "Write a slightly spooky everyday moment — mild, not graphic.",
    "bright": "Write a cheerful, concrete everyday scene.",
}

_PUSH_SYSTEM = (
    "You write one tiny English snippet that helps a learner understand a "
    "given vocabulary word. "
    "The target word MUST appear literally in your reply (same spelling, "
    "case-insensitive) — never replace it with a synonym, even if it is rare "
    "or advanced. "
    "Around it, use simple, everyday English (CEFR A2–B1 level): only common, "
    "easy words and plain grammar. "
    "Keep it to 1–2 short sentences, at most 25 words total. "
    "Build the snippet around a clear, concrete situation so that someone who "
    "has never seen the target word can guess its meaning from the context. "
    "No headings, no quotes, no explanation — just the snippet."
)

_EXPLAIN_SYSTEM = (
    "You are a concise English tutor. In ONE short sentence, explain in plain "
    "English what the given word or phrase means. Then give ONE short example "
    "sentence that uses it. Two sentences total. No headings, no quotes, no lists."
)

_GRADE_JUDGE_SYSTEM = (
    "You judge whether a user-typed translation is acceptable. "
    "Accept inflection variants, minor synonyms, and missing or extra "
    "prepositions when the meaning is preserved. Reject if the meaning "
    "differs or essential content is missing. "
    "Reply with exactly YES or NO — nothing else."
)

_JUST_TALK_TAIL = "\nKeep answers short: 1–2 paragraphs at most."

_JUST_TALK_VOCAB = (
    "\nThe user is studying an English vocabulary list. "
    "If any of these words fit naturally in your reply, use them: {words}. "
    "Do not force words in; skip them entirely when they don't fit the answer."
)


def resolve_tone(tone: str, rng: random.Random | None = None) -> str:
    """Expand "mixed" to a concrete tone; validate otherwise."""
    if tone == "mixed":
        return (rng or random).choice(TONES)
    if tone not in _TONE_INSTRUCTIONS:
        raise ValueError(f"unknown tone: {tone!r}")
    return tone


def push_messages(
    word: str,
    tone: str,
    *,
    rng: random.Random | None = None,
) -> list[dict]:
    """Build the messages payload for a scheduled push using `word`."""
    resolved = resolve_tone(tone, rng=rng)
    return [
        {
            "role": "system",
            "content": f"{_PUSH_SYSTEM}\n{_TONE_INSTRUCTIONS[resolved]}",
        },
        {
            "role": "user",
            "content": (
                f"Word to use: {word}\n"
                f'Remember: the exact word "{word}" must appear in the snippet.'
            ),
        },
    ]


def explain_messages(word: str) -> list[dict]:
    """Build messages asking for a brief meaning + one example for `word`.

    Used when the user taps ❌ forgot on a push — we follow up with a tiny
    definition so they can learn it on the spot.
    """
    return [
        {"role": "system", "content": _EXPLAIN_SYSTEM},
        {"role": "user", "content": f"Word or phrase: {word}"},
    ]


def grade_translation_messages(
    prompt: str, expected: str, user_text: str
) -> list[dict]:
    """Build YES/NO judge messages for grading a typed translation answer.

    Used by the Repeat (typed) game to tolerate inflection / synonyms /
    missing prepositions — the LLM is the rubric, not string equality.
    """
    user = (
        f"Prompt shown to user: {prompt}\n"
        f"Expected answer: {expected}\n"
        f"User typed: {user_text}\n\n"
        "Acceptable?"
    )
    return [
        {"role": "system", "content": _GRADE_JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


def just_talk_system(base: str, vocab_words: list[str]) -> str:
    """Compose the system prompt for a plain-text chat reply.

    Appends a short note reminding the model to keep replies tight and — if the
    user has vocabulary loaded — to use those words when they fit naturally.
    """
    prompt = base + _JUST_TALK_TAIL
    if vocab_words:
        prompt += _JUST_TALK_VOCAB.format(words=", ".join(vocab_words))
    return prompt
