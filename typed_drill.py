"""Typed-answer translation drill — one game, two word sources.

The "Typed drill" `/games` button drills the chat's `/focus`-scoped
in-progress words (strict grading, ``source="game"``) and salts in up to
``MAX_SALT`` `remembered`-but-not-`mastered` words per game as mastery
checks. Salted rounds carry ``Round.judged=True``: bot.py grades them with
the tolerant LLM judge and records ``source="repeat"``, which drives the
forget-flip / mastered machinery. Everything here is pure except
``grade_answer_llm`` (one LLM call); no telegram imports, no DB writes —
`bot.py` wires the pool queries, plain-text input routing, and the
end-of-session summary. Game state is per-chat and held only in process
memory; a bot restart silently abandons in-flight games.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Literal

import llm
import prompts

MIN_ROUNDS = 5         # smallest translatable focus pool the drill accepts
MAX_ROUNDS = 10        # total rounds cap (focus + salt)
MAX_SALT = 2           # remembered words salted in per game as mastery checks
JUDGE_TIMEOUT_S = 10   # the whole bot blocks on the judge (PTB is sequential)

Direction = Literal["en2ru", "ru2en"]


@dataclass
class Round:
    word_id: int
    prompt: str
    expected: str
    direction: Direction
    # True for salted `remembered` words: LLM-judged, recorded as
    # source="repeat" so a miss forget-flips and a streak masters.
    judged: bool = False


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


def _filter_pool(rows: Iterable) -> list[tuple[int, str, str]]:
    pool: list[tuple[int, str, str]] = []
    for r in rows:
        v = _row_view(r)
        if v is not None:
            pool.append(v)
    return pool


def _make_round(
    entry: tuple[int, str, str], *, judged: bool, rng: random.Random
) -> Round:
    wid, text, translation = entry
    direction: Direction = "en2ru" if rng.random() < 0.5 else "ru2en"
    if direction == "en2ru":
        prompt, expected = text, translation
    else:
        prompt, expected = translation, text
    return Round(
        word_id=wid,
        prompt=prompt,
        expected=expected,
        direction=direction,
        judged=judged,
    )


def draw_rounds(
    focus_rows: Iterable,
    remembered_rows: Iterable = (),
    *,
    n_max: int = MAX_ROUNDS,
    n_salt: int = MAX_SALT,
    min_rounds: int = MIN_ROUNDS,
    rng: random.Random | None = None,
) -> list[Round]:
    """Draw one Typed-drill game: focus-pool rounds plus salted mastery checks.

    Rows whose translation is None/empty/whitespace are filtered from both
    pools. The focus pool must keep at least ``min_rounds`` rows or
    ``ValueError`` is raised — the salt never counts toward the minimum.
    Up to ``n_salt`` remembered rows become ``judged=True`` rounds; focus
    rounds fill the rest of the ``n_max`` budget (``min(n_max - salt,
    pool)``). All sampling is without replacement, each round's direction
    is chosen independently from ``rng``, and the final order is shuffled
    so the mastery checks don't cluster at the end.
    """
    if min_rounds <= 0:
        raise ValueError(f"min_rounds must be positive, got {min_rounds}")
    if n_max < min_rounds:
        raise ValueError(
            f"n_max ({n_max}) must be >= min_rounds ({min_rounds})"
        )
    if n_salt < 0:
        raise ValueError(f"n_salt must be >= 0, got {n_salt}")
    rng = rng or random.Random()
    focus_pool = _filter_pool(focus_rows)
    if len(focus_pool) < min_rounds:
        raise ValueError(
            f"need at least {min_rounds} translatable rows, got {len(focus_pool)}"
        )
    # A word in both pools plays as a focus round, not a mastery check.
    focus_ids = {wid for wid, _, _ in focus_pool}
    salt_pool = [
        e for e in _filter_pool(remembered_rows) if e[0] not in focus_ids
    ]
    n_salt_actual = min(n_salt, len(salt_pool))
    n_focus = min(n_max - n_salt_actual, len(focus_pool))
    out = [
        _make_round(e, judged=False, rng=rng)
        for e in rng.sample(focus_pool, n_focus)
    ]
    out += [
        _make_round(e, judged=True, rng=rng)
        for e in rng.sample(salt_pool, n_salt_actual)
    ]
    rng.shuffle(out)
    return out


def grade_answer(user_text: str, rd: Round) -> bool:
    """Case-insensitive, whitespace-trimmed equality vs ``rd.expected``."""
    return user_text.strip().casefold() == rd.expected.strip().casefold()


async def grade_answer_llm(user_text: str, rd: Round) -> bool | None:
    """Tolerant grading via an LLM yes/no judge with a strict-equality fast path.

    Returns True when the case-folded / whitespace-stripped ``user_text``
    equals ``rd.expected`` (no LLM call), OR when ``llm.chat`` resolves to a
    reply whose first non-whitespace token starts with ``YES``
    (case-insensitive). A reply that starts with anything else is a real
    judgment → False. Returns ``None`` when the judge was UNAVAILABLE
    (transport error or empty reply) — the caller must degrade gracefully:
    treat the round as strict-wrong for scoring, but skip consequences that
    assume a real judgment (e.g. demoting a `remembered` word). The call is
    capped at ``JUDGE_TIMEOUT_S`` because PTB processes updates sequentially
    — a hung backend would otherwise freeze every chat.
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
            timeout=JUDGE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — judge unavailable, not a judgment
        return None
    if not reply.strip():
        return None
    head = reply.strip().split(None, 1)[0].upper()
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
