"""In-memory game scaffolding for vocab-driven mini-games.

Two directions, one engine: ``draw_rounds(direction="wt")`` builds a
Word→Translation quiz, ``direction="tw"`` swaps the prompt and option
columns to make a Translation→Word quiz. ``apply_answer`` and
``format_result`` are direction-agnostic. Game state is per-chat and
held only in process memory; a bot restart silently abandons in-flight
games. No telegram imports — `bot.py` does the wiring.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

MIN_VOCAB = 4
N_OPTIONS = 4
MAX_ROUNDS = 10


@dataclass
class Round:
    word_id: int
    prompt: str
    correct_answer: str
    options: list[str]
    correct_index: int


@dataclass
class Game:
    chat_id: int
    rounds: list[Round]
    score: int = 0
    current_round: int = 0

    @property
    def n_rounds(self) -> int:
        return len(self.rounds)

    @property
    def done(self) -> bool:
        return self.current_round >= len(self.rounds)

    def current(self) -> Round:
        return self.rounds[self.current_round]


def _row_view(row) -> tuple[int, str, str] | None:
    """Coerce a sqlite3.Row or (id, text, translation) tuple into a tuple,
    or return None if the translation is missing/empty."""
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
    direction: str = "wt",
    n_max: int = MAX_ROUNDS,
    rng: random.Random | None = None,
) -> list[Round]:
    """Sample correct words and build per-round 4-option button sets.

    ``direction="wt"`` builds Word→Translation rounds (prompt = English
    text, options = translations). ``direction="tw"`` builds the mirror
    Translation→Word (prompt = translation, options = English texts).

    Rows whose translation is None/empty/whitespace are skipped (in either
    direction — the translation is needed as the prompt for "tw" and as
    the answer for "wt"). The surviving pool must contain at least
    ``MIN_VOCAB`` rows or ``ValueError`` is raised. Each round's three
    distractors are sampled without replacement from the rest of the
    chosen pool — so words used as the correct answer in earlier rounds
    may legitimately reappear as distractors later.
    """
    if direction not in ("wt", "tw"):
        raise ValueError(
            f"unknown direction {direction!r}, expected 'wt' or 'tw'"
        )
    rng = rng or random.Random()
    pool: list[tuple[int, str, str]] = []
    for r in rows:
        v = _row_view(r)
        if v is not None:
            pool.append(v)
    if len(pool) < MIN_VOCAB:
        raise ValueError(
            f"need at least {MIN_VOCAB} translatable rows, got {len(pool)}"
        )
    n_rounds = min(n_max, len(pool))
    chosen = rng.sample(pool, n_rounds)
    # Column 1 = English text, column 2 = translation. The "answer" column
    # is the side the user picks from buttons; the "prompt" column is the
    # side displayed as the round question.
    answer_col = 2 if direction == "wt" else 1
    prompt_col = 1 if direction == "wt" else 2
    out: list[Round] = []
    for i, row in enumerate(chosen):
        wid = row[0]
        prompt = row[prompt_col]
        correct_answer = row[answer_col]
        candidates = [chosen[j][answer_col] for j in range(len(chosen)) if j != i]
        distractors = rng.sample(candidates, N_OPTIONS - 1)
        # Permute indices, not strings, so duplicate answer values
        # (e.g. couch/sofa → диван in "wt") don't confuse correct_index.
        slots = [correct_answer] + distractors
        order = list(range(len(slots)))
        rng.shuffle(order)
        options = [slots[k] for k in order]
        correct_index = order.index(0)
        out.append(
            Round(
                word_id=wid,
                prompt=prompt,
                correct_answer=correct_answer,
                options=options,
                correct_index=correct_index,
            )
        )
    return out


def apply_answer(game: Game, chosen_index: int) -> bool:
    """Resolve a tap on the current round.

    Returns True if ``chosen_index`` matches the round's ``correct_index``;
    increments score on correct and advances ``current_round`` either way.
    A no-op (returns False) once the game is already done.
    """
    if game.done:
        return False
    rd = game.rounds[game.current_round]
    correct = chosen_index == rd.correct_index
    if correct:
        game.score += 1
    game.current_round += 1
    return correct


def format_result(score: int, n_rounds: int) -> str:
    return f"🎯 You scored {score}/{n_rounds}"
