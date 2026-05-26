"""Typed-answer "Repeat" mini-game for remembered-only words.

Drills the chat's `remembered`-labelled words by prompting the user to type
the translation (either direction, chosen per round). Pure helpers — no
telegram imports, no DB writes; `bot.py` wires the pool query, plain-text
input routing, and end-of-session summary. Game state is per-chat and held
only in process memory; a bot restart silently abandons in-flight games.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Literal

import llm
import prompts

N_ROUNDS = 5
Direction = Literal["en2ru", "ru2en"]


@dataclass
class Round:
    word_id: int
    prompt: str
    expected: str
    direction: Direction


@dataclass
class Game:
    chat_id: int
    rounds: list[Round]
    score: int = 0
    current_round: int = 0
    wrong: list[str] = field(default_factory=list)

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    @property
    def done(self) -> bool:
        return self.current_round >= len(self.rounds)

    def current(self) -> Round:
        return self.rounds[self.current_round]


def _row_view(row) -> tuple[int, str, str] | None:
    if hasattr(row, "keys"):
        rid = row["id"]
        text = row["text"]
        translation = row["translation"]
    else:
        rid, text, translation = row
    if translation is None:
        return None
    translation = str(translation).strip()
    if not translation:
        return None
    return int(rid), str(text), translation


def draw_rounds(
    rows: Iterable,
    *,
    n_rounds: int = N_ROUNDS,
    rng: random.Random | None = None,
) -> list[Round]:
    """Sample ``n_rounds`` rows and assign a random direction per round.

    Rows whose translation is None/empty/whitespace are filtered first; if
    fewer than ``n_rounds`` translatable rows remain (or ``n_rounds`` is
    non-positive), ``ValueError`` is raised. Each round's direction is
    chosen independently from ``rng``.
    """
    if n_rounds <= 0:
        raise ValueError(f"n_rounds must be positive, got {n_rounds}")
    rng = rng or random.Random()
    pool: list[tuple[int, str, str]] = []
    for r in rows:
        v = _row_view(r)
        if v is not None:
            pool.append(v)
    if len(pool) < n_rounds:
        raise ValueError(
            f"need at least {n_rounds} translatable rows, got {len(pool)}"
        )
    chosen = rng.sample(pool, n_rounds)
    out: list[Round] = []
    for wid, text, translation in chosen:
        direction: Direction = "en2ru" if rng.random() < 0.5 else "ru2en"
        if direction == "en2ru":
            prompt, expected = text, translation
        else:
            prompt, expected = translation, text
        out.append(
            Round(
                word_id=wid,
                prompt=prompt,
                expected=expected,
                direction=direction,
            )
        )
    return out


def grade_answer(user_text: str, rd: Round) -> bool:
    """Case-insensitive, whitespace-trimmed equality vs ``rd.expected``."""
    return user_text.strip().casefold() == rd.expected.strip().casefold()


async def grade_answer_llm(user_text: str, rd: Round) -> bool:
    """Tolerant grading via an LLM yes/no judge with a strict-equality fast path.

    Returns True when the case-folded / whitespace-stripped ``user_text``
    equals ``rd.expected`` (no LLM call), OR when ``llm.chat`` resolves to a
    reply whose first non-whitespace token equals ``YES`` (case-insensitive).
    Any other path — explicit ``NO``, unparseable reply, or transport error
    from ``llm.chat`` — yields False, so a flaky backend degrades to strict
    equality rather than auto-accepting.
    """
    if grade_answer(user_text, rd):
        return True
    try:
        reply = await llm.chat(
            prompts.grade_translation_messages(
                rd.prompt, rd.expected, user_text
            ),
            max_tokens=4,
            temperature=0.0,
            disable_reasoning=True,
        )
    except Exception:  # noqa: BLE001 — any failure falls back to strict
        return False
    head = reply.strip().split(None, 1)[0].upper() if reply.strip() else ""
    return head.startswith("YES")


def apply_answer(game: Game, correct: bool, *, source_word: str) -> None:
    """Advance the round, increment score, record wrong-word on miss.

    ``source_word`` is the English vocab text of the round (so the summary
    can name the word regardless of direction). No-op once ``game.done``.
    """
    if game.done:
        return
    if correct:
        game.score += 1
    else:
        game.wrong.append(source_word)
    game.current_round += 1


def format_result(score: int, n_rounds: int, wrong: list[str]) -> str:
    head = f"🎯 You scored {score}/{n_rounds}"
    if not wrong:
        return head
    return head + "\nWrong: " + ", ".join(wrong)
