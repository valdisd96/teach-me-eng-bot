"""Static irregular-verb table + "Type the trio" mini-game scaffolding.

Same verb set for every chat — no DB persistence, no per-chat customisation.
The game prompts the user with the bare base form (e.g. ``go``) and grades a
free-text reply that should contain the past simple and past participle
(``went / gone``). State is per-chat and held only in process memory; a bot
restart silently abandons in-flight games. No telegram imports — `bot.py`
does the wiring.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Iterable

MAX_ROUNDS = 10

# (base, past_simple_alts, past_participle_alts).
# Multiple accepted forms for each tense — tests grade case-insensitively
# against the alt list, so spelling variants like "learned"/"learnt" both pass.
IRREGULAR_VERBS: list[tuple[str, list[str], list[str]]] = [
    ("become", ["became"], ["become"]),
    ("begin", ["began"], ["begun"]),
    ("bend", ["bent"], ["bent"]),
    ("bite", ["bit"], ["bitten"]),
    ("bleed", ["bled"], ["bled"]),
    ("blow", ["blew"], ["blown"]),
    ("break", ["broke"], ["broken"]),
    ("bring", ["brought"], ["brought"]),
    ("build", ["built"], ["built"]),
    ("burn", ["burned", "burnt"], ["burned", "burnt"]),
    ("buy", ["bought"], ["bought"]),
    ("catch", ["caught"], ["caught"]),
    ("choose", ["chose"], ["chosen"]),
    ("come", ["came"], ["come"]),
    ("cost", ["cost"], ["cost"]),
    ("cut", ["cut"], ["cut"]),
    ("do", ["did"], ["done"]),
    ("draw", ["drew"], ["drawn"]),
    ("dream", ["dreamed", "dreamt"], ["dreamed", "dreamt"]),
    ("drink", ["drank"], ["drunk"]),
    ("drive", ["drove"], ["driven"]),
    ("eat", ["ate"], ["eaten"]),
    ("fall", ["fell"], ["fallen"]),
    ("feel", ["felt"], ["felt"]),
    ("fight", ["fought"], ["fought"]),
    ("find", ["found"], ["found"]),
    ("fly", ["flew"], ["flown"]),
    ("forget", ["forgot"], ["forgotten", "forgot"]),
    ("get", ["got"], ["gotten", "got"]),
    ("give", ["gave"], ["given"]),
    ("go", ["went"], ["gone"]),
    ("grow", ["grew"], ["grown"]),
    ("have", ["had"], ["had"]),
    ("hear", ["heard"], ["heard"]),
    ("hide", ["hid"], ["hidden"]),
    ("hit", ["hit"], ["hit"]),
    ("hold", ["held"], ["held"]),
    ("keep", ["kept"], ["kept"]),
    ("know", ["knew"], ["known"]),
    ("learn", ["learned", "learnt"], ["learned", "learnt"]),
    ("leave", ["left"], ["left"]),
    ("lend", ["lent"], ["lent"]),
    ("let", ["let"], ["let"]),
    ("lose", ["lost"], ["lost"]),
    ("make", ["made"], ["made"]),
    ("mean", ["meant"], ["meant"]),
    ("meet", ["met"], ["met"]),
    ("pay", ["paid"], ["paid"]),
    ("put", ["put"], ["put"]),
    ("read", ["read"], ["read"]),
    ("ride", ["rode"], ["ridden"]),
    ("ring", ["rang"], ["rung"]),
    ("rise", ["rose"], ["risen"]),
    ("run", ["ran"], ["run"]),
    ("say", ["said"], ["said"]),
    ("see", ["saw"], ["seen"]),
    ("sell", ["sold"], ["sold"]),
    ("send", ["sent"], ["sent"]),
    ("set", ["set"], ["set"]),
    ("shake", ["shook"], ["shaken"]),
    ("shut", ["shut"], ["shut"]),
    ("sing", ["sang"], ["sung"]),
    ("sit", ["sat"], ["sat"]),
    ("sleep", ["slept"], ["slept"]),
    ("smell", ["smelled", "smelt"], ["smelled", "smelt"]),
    ("speak", ["spoke"], ["spoken"]),
    ("spell", ["spelled", "spelt"], ["spelled", "spelt"]),
    ("spend", ["spent"], ["spent"]),
    ("stand", ["stood"], ["stood"]),
    ("steal", ["stole"], ["stolen"]),
    ("swim", ["swam"], ["swum"]),
    ("take", ["took"], ["taken"]),
    ("teach", ["taught"], ["taught"]),
    ("tell", ["told"], ["told"]),
    ("think", ["thought"], ["thought"]),
    ("throw", ["threw"], ["thrown"]),
    ("understand", ["understood"], ["understood"]),
    ("wake", ["woke"], ["woken"]),
    ("wear", ["wore"], ["worn"]),
    ("win", ["won"], ["won"]),
    ("write", ["wrote"], ["written"]),
]


@dataclass
class Round:
    base: str
    past_simple_alts: list[str]
    past_participle_alts: list[str]


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


def draw_rounds(
    verbs: Iterable[tuple[str, list[str], list[str]]] = IRREGULAR_VERBS,
    *,
    n_max: int = MAX_ROUNDS,
    rng: random.Random | None = None,
) -> list[Round]:
    """Sample ``min(n_max, len(verbs))`` verbs without replacement.

    Raises ``ValueError`` on an empty pool or non-positive ``n_max``.
    """
    if n_max <= 0:
        raise ValueError(f"n_max must be positive, got {n_max}")
    pool = list(verbs)
    if not pool:
        raise ValueError("verb pool is empty")
    rng = rng or random.Random()
    n_rounds = min(n_max, len(pool))
    chosen = rng.sample(pool, n_rounds)
    return [
        Round(base=base, past_simple_alts=list(ps), past_participle_alts=list(pp))
        for base, ps, pp in chosen
    ]


_SEPARATORS = re.compile(r"[/,\s]+")


def grade_answer(text: str, rd: Round) -> tuple[bool, str | None]:
    """Parse free-text input as ``<past_simple> <past_participle>`` and grade.

    Splits ``text`` on any combination of slash, comma, or whitespace,
    drops empty fragments, and lowercases each part. Wrong arity returns
    ``(False, None)``; correct arity returns ``(matched, "<ps> / <pp>")``
    where ``matched`` is True iff each part is in the corresponding alt
    list (compared case-insensitively).
    """
    parts = [p for p in _SEPARATORS.split(text.strip().lower()) if p]
    if len(parts) != 2:
        return False, None
    user_ps, user_pp = parts
    ps_set = {a.lower() for a in rd.past_simple_alts}
    pp_set = {a.lower() for a in rd.past_participle_alts}
    correct = user_ps in ps_set and user_pp in pp_set
    return correct, f"{user_ps} / {user_pp}"


def apply_answer(game: Game, correct: bool) -> None:
    """Advance the round, incrementing ``score`` only on a correct answer.

    No-op once the game is already done.
    """
    if game.done:
        return
    if correct:
        game.score += 1
    game.current_round += 1


def format_result(score: int, n_rounds: int) -> str:
    return f"🎯 You scored {score}/{n_rounds}"
