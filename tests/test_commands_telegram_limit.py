"""Tests for issue #108 — `/games` COMMANDS description must stay <= Telegram's 256-char ceiling.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. Behaviour
asserted here is derived from that spec, not from `bot.py` bodies. The COMMANDS
list is module-level public data already exercised by `tests/test_help.py`.
"""

from __future__ import annotations

import os

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)


# Telegram's setMyCommands API rejects any description longer than this.
TELEGRAM_DESC_MAX = 256


# Snapshot of every COMMANDS entry as it stood BEFORE issue #108's fix, except
# `"games"` (whose description is the one being rewritten). AC5: drive-by edits
# to any other entry would diverge from this snapshot and fail.
EXPECTED_NON_GAMES_ENTRIES: list[tuple[str, str]] = [
    ("start", "Configure schedule: timezone, pushes/day, active window, tone, target language"),
    ("help", "Show this help message"),
    ("add", "Add a word or phrase to this chat's vocab"),
    ("remove", "Remove a word or phrase from vocab"),
    ("list", "List vocab words (every row shows its labels; optional label spec filter, AND across tokens; prepend --any for OR)"),
    ("import", "Bulk-import vocab from a CSV file (one word per row)"),
    ("export", "Download this chat's vocab as a CSV file"),
    ("resetvocab", "Wipe this chat's vocabulary (with confirm)"),
    ("tr", "Translate args; tap the button under the reply to add the English word/phrase to vocab"),
    ("label", "Attach labels to a vocab word (e.g. /label horse pos:noun type:animal)"),
    ("unlabel", "Detach labels from a vocab word"),
    ("labels", "List every label in this chat with its attached-word count"),
    ("focus", "Set a sticky label spec scoping pushes + post-/forgot game button (e.g. /focus pos:noun, AND across tokens; prepend --any for OR, e.g. /focus --any type:body type:medicine); /focus clear removes it; /focus echoes current"),
    ("clear", "Reset the chat history (LLM memory)"),
    ("status", "Show host diagnostics, vocab count, and a short model bench"),
]


EXPECTED_NAMES_IN_ORDER: list[str] = [
    "start", "help", "add", "remove", "list", "import", "export",
    "resetvocab", "tr", "games", "label", "unlabel", "labels",
    "focus", "clear", "status",
]


def _games_desc() -> str:
    desc = next((d for n, d in bot.COMMANDS if n == "games"), None)
    assert desc is not None, "COMMANDS must contain a 'games' entry"
    return desc


def test_all_commands_descriptions_within_256_chars() -> None:  # AC1, edge: boundary `<=` not `<`
    """Every (name, desc) in bot.COMMANDS must satisfy len(desc.encode("utf-8")) <= 256.

    Telegram's setMyCommands API enforces the 256 limit on the UTF-8 byte length
    of the description, not the Unicode code-point count. A previous fix passed
    a char-length check but still tripped the wire-level byte check (#110).
    Boundary direction: exactly 256 bytes must pass (hence `<= 256`, not `< 256`).
    """
    offenders = [
        (name, len(desc.encode("utf-8")))
        for name, desc in bot.COMMANDS
        if len(desc.encode("utf-8")) > TELEGRAM_DESC_MAX
    ]
    assert not offenders, (
        f"AC1: every COMMANDS description must be <= {TELEGRAM_DESC_MAX} UTF-8 bytes; "
        f"offenders (name, byte-length): {offenders}"
    )


def test_games_description_mentions_cancel() -> None:  # AC2 — case-insensitive substring "cancel"
    desc = _games_desc()
    assert "cancel" in desc.lower(), (
        f"AC2: /games description must still advertise the cancel sub-command; got {desc!r}"
    )


def test_games_description_mentions_label_filter() -> None:  # AC3 — case-insensitive "label" or "filter"
    desc = _games_desc().lower()
    assert "label" in desc or "filter" in desc, (
        f"AC3: /games description must still mention the label-spec filter; got {desc!r}"
    )


def test_games_description_mentions_irregular_verbs() -> None:  # AC4 — case-insensitive substring "irregular"
    desc = _games_desc()
    assert "irregular" in desc.lower(), (
        f"AC4: /games description must still mention irregular verbs; got {desc!r}"
    )


def test_non_games_entries_byte_for_byte_unchanged() -> None:  # AC5 — drive-by edit guard
    """Every COMMANDS entry except `games` must match its pre-#108 string exactly."""
    actual_non_games = [(n, d) for n, d in bot.COMMANDS if n != "games"]
    assert actual_non_games == EXPECTED_NON_GAMES_ENTRIES, (
        "AC5: only the /games description should change in #108. Diff:\n"
        f"  expected: {EXPECTED_NON_GAMES_ENTRIES}\n"
        f"  actual:   {actual_non_games}"
    )


def test_commands_names_and_ordering_preserved() -> None:  # AC7 — documented order preserved
    actual_names = [name for name, _ in bot.COMMANDS]
    assert actual_names == EXPECTED_NAMES_IN_ORDER, (
        f"AC7: COMMANDS names must appear in the documented order; "
        f"expected {EXPECTED_NAMES_IN_ORDER}, got {actual_names}"
    )


def test_games_description_is_pure_ascii() -> None:  # AC6 — pure-ASCII drift guard
    """The /games description must be pure ASCII so future char/byte counts agree.

    Non-ASCII glyphs (e.g. `→`, `–`, em-dash, accents) cost multiple UTF-8 bytes
    each and silently inflate the wire-level byte length past 256 even when the
    code-point count looks safe — exactly how #110 happened. Locking the string
    to ASCII makes len(desc) and len(desc.encode("utf-8")) coincide.
    """
    desc = _games_desc()
    try:
        desc.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AssertionError(
            f"AC6: /games description must be pure ASCII; non-ASCII char at offset "
            f"{exc.start}..{exc.end} ({desc[exc.start:exc.end]!r}); full desc: {desc!r}"
        ) from None
