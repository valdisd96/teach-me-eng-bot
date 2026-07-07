"""Dictionary spell-check for /add.

A typo baked into vocab gets force-woven into every future story
("humiliationg" shipped verbatim in production) — catch it at entry with a
plain word list, not an LLM (issue #101 removed add-time LLM suggestions
deliberately). `suggest` is sync and loads the dictionary lazily on first
use (~0.3 s); bot.py calls it via `asyncio.to_thread`.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _checker():
    from spellchecker import SpellChecker

    return SpellChecker()


def suggest(word: str) -> str | None:
    """A correction for a single misspelled English word, or None.

    None means "don't second-guess the user": multi-token phrases, tokens
    with anything besides letters and inner apostrophes (names, numbers,
    hyphenations), dictionary-known words, and unknowns without a good
    candidate all pass through silently.
    """
    w = word.strip().lower()
    if not w or " " in w:
        return None
    if not all(c.isalpha() or c == "'" for c in w):
        return None
    checker = _checker()
    if checker.known([w]):
        return None
    correction = checker.correction(w)
    if correction and correction != w:
        return correction
    return None
