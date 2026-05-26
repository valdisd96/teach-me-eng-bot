"""Typed-answer "Focus drill" mini-game for in-progress focus words.

Drills the chat's `/focus`-scoped, non-`remembered` words by prompting the
user to type the translation (random direction per round). Same pure-helper
shape as `repeat_game.py` — no telegram imports, no DB writes; `bot.py`
wires the pool query, plain-text input routing, and end-of-session summary.
Game state is per-chat and held only in process memory; a bot restart
silently abandons in-flight games.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Literal

MIN_ROUNDS = 5
MAX_ROUNDS = 5
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
    n_max: int = MAX_ROUNDS,
    min_rounds: int = MIN_ROUNDS,
    rng: random.Random | None = None,
) -> list[Round]:
    """Sample up to ``n_max`` translatable rows and pick a direction per round.

    Rows whose translation is None/empty/whitespace are filtered first; the
    surviving pool must contain at least ``min_rounds`` rows or ``ValueError``
    is raised. The round count is ``min(n_max, len(pool))``. Each round's
    direction is chosen independently from ``rng``.
    """
    if min_rounds <= 0:
        raise ValueError(f"min_rounds must be positive, got {min_rounds}")
    if n_max < min_rounds:
        raise ValueError(
            f"n_max ({n_max}) must be >= min_rounds ({min_rounds})"
        )
    rng = rng or random.Random()
    pool: list[tuple[int, str, str]] = []
    for r in rows:
        v = _row_view(r)
        if v is not None:
            pool.append(v)
    if len(pool) < min_rounds:
        raise ValueError(
            f"need at least {min_rounds} translatable rows, got {len(pool)}"
        )
    n_rounds = min(n_max, len(pool))
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
