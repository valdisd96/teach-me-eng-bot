"""Daily cloze-story session: pure scaffolding.

Once a day the bot sends one short LLM story where the day's selected words
are replaced by numbered blanks; the user fills them one by one and each
answer feeds FSRS. This module owns the blanking, grading, session state,
and message formatting — no telegram imports, no DB writes, no LLM calls;
`scheduler.compose_session` builds sessions and `bot.py` wires delivery and
plain-text answer routing. State is per-chat and held only in process
memory; a bot restart silently abandons an in-flight session.
"""

from __future__ import annotations

import html
import random
import re
from dataclasses import dataclass, field

# At most this many introduction-phase words (reps < INTRO_GRADUATION_REPS)
# are seeded into each daily session, bypassing /focus — the session-based
# replacement for the old every-3rd-push introduction slots.
MAX_INTRO_WORDS = 2


@dataclass
class Blank:
    word_id: int
    word: str
    is_intro: bool
    translation: str | None = None


@dataclass
class Session:
    chat_id: int
    story: str  # raw story text, words in place
    display: str  # story with ___(n) blanks, plain text
    blanks: list[Blank]  # in blank-number (appearance) order
    push_id: int | None = None
    score: int = 0
    current_blank: int = 0
    wrong: list[str] = field(default_factory=list)

    @property
    def n_blanks(self) -> int:
        return len(self.blanks)

    @property
    def done(self) -> bool:
        return self.current_blank >= len(self.blanks)

    def current(self) -> Blank:
        return self.blanks[self.current_blank]


def _find_span(haystack_cf: str, word: str, taken: list[tuple[int, int]]) -> tuple[int, int] | None:
    """First case-insensitive whole-word match of `word` not overlapping `taken`."""
    pattern = re.compile(
        r"(?<![A-Za-z])" + re.escape(word.casefold()) + r"(?![A-Za-z])"
    )
    for m in pattern.finditer(haystack_cf):
        span = (m.start(), m.end())
        if all(span[1] <= s or span[0] >= e for s, e in taken):
            return span
    return None


def blank_story(
    story: str, words: list[str]
) -> tuple[str, list[int], list[str]]:
    """Replace each word's first occurrence in `story` with a numbered blank.

    Returns `(display, order, missing)` — `display` is the story with
    `___(n)` markers numbered 1..K in order of appearance, `order` maps
    blank number n → index into `words` (so `order[0]` is the word behind
    blank 1), and `missing` lists words that never appeared. Longer words
    are matched first so a phrase is never shadowed by one of its own
    sub-words, and spans never overlap.
    """
    haystack = story.casefold()
    spans: dict[int, tuple[int, int]] = {}
    taken: list[tuple[int, int]] = []
    for idx in sorted(
        range(len(words)), key=lambda i: len(words[i]), reverse=True
    ):
        needle = words[idx].casefold()
        span = _find_span(haystack, needle, taken)
        if span is None and needle.startswith("to "):
            # The story may legitimately use an infinitive-marked vocab entry
            # without the "to" (e.g. "she ran out of milk" for "to run out
            # of"'s bare form) — accept the bare phrase as the blank.
            span = _find_span(haystack, needle[3:], taken)
        if span is not None:
            spans[idx] = span
            taken.append(span)
    missing = [w for i, w in enumerate(words) if i not in spans]
    order = sorted(spans, key=lambda i: spans[i][0])
    out: list[str] = []
    cursor = 0
    for n, idx in enumerate(order, start=1):
        s, e = spans[idx]
        out.append(story[cursor:s])
        out.append(f"___({n})")
        cursor = e
    out.append(story[cursor:])
    return "".join(out), order, missing


def _normalize_answer(s: str) -> str:
    s = " ".join(s.strip().casefold().split())
    if s.startswith("to "):
        s = s[3:]
    return s


def grade_answer(user_text: str, expected: str) -> bool:
    """Case/whitespace-insensitive match; a leading "to " is optional on
    either side so `run out of` matches the stored `to run out of`."""
    return _normalize_answer(user_text) == _normalize_answer(expected)


def apply_answer(session: Session, correct: bool) -> None:
    """Record the current blank's outcome and advance to the next one."""
    blank = session.current()
    if correct:
        session.score += 1
    else:
        session.wrong.append(blank.word)
    session.current_blank += 1


def format_session_message(
    session: Session, *, rng: random.Random | None = None
) -> str:
    """The daily push body: header, 🆕 intro words, blanked story, shuffled
    word bank, and the first answer prompt. HTML-safe."""
    lines = [f"📖 <b>Daily story</b> — fill in {session.n_blanks} blanks"]
    intro = [b for b in session.blanks if b.is_intro]
    if intro:
        pairs = ", ".join(
            f"<b>{html.escape(b.word)}</b>"
            + (f" (<i>{html.escape(b.translation)}</i>)" if b.translation else "")
            for b in intro
        )
        lines.append(f"\n🆕 New words to meet: {pairs}")
    lines.append(f"\n{html.escape(session.display)}")
    bank = [b.word for b in session.blanks]
    (rng or random).shuffle(bank)
    lines.append(
        "\nWord bank: " + " · ".join(html.escape(w) for w in bank)
    )
    lines.append(f"\n{format_blank_prompt(session)}")
    return "\n".join(lines)


def format_blank_prompt(session: Session) -> str:
    return (
        f"✍️ Type the word for blank ({session.current_blank + 1}) "
        f"of {session.n_blanks}."
    )


def format_answer_feedback(correct: bool, expected: str) -> str:
    return f"✅ {expected}" if correct else f"❌ it was: {expected}"


def _bold_words(story: str, words: list[str]) -> str:
    """HTML-escape `story` and wrap each word's first occurrence in <b>…</b>,
    using the same span discovery as `blank_story` so the bolded words are
    exactly the ones that were blanked."""
    haystack = story.casefold()
    taken: list[tuple[int, int]] = []
    for w in sorted(words, key=len, reverse=True):
        span = _find_span(haystack, w.casefold(), taken)
        if span is not None:
            taken.append(span)
    out: list[str] = []
    cursor = 0
    for s, e in sorted(taken):
        out.append(html.escape(story[cursor:s]))
        out.append(f"<b>{html.escape(story[s:e])}</b>")
        cursor = e
    out.append(html.escape(story[cursor:]))
    return "".join(out)


def format_result(session: Session) -> str:
    """End-of-session message: the completed story with the day's words
    bolded, the score, and translations of the missed words. HTML-safe."""
    words = [b.word for b in session.blanks]
    lines = [_bold_words(session.story, words)]
    lines.append(f"\n🎯 You scored {session.score}/{session.n_blanks}")
    missed = [b for b in session.blanks if b.word in session.wrong]
    for b in missed:
        tr = f" — <i>{html.escape(b.translation)}</i>" if b.translation else ""
        lines.append(f"❌ <b>{html.escape(b.word)}</b>{tr}")
    return "\n".join(lines)
