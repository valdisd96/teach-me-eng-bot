"""Tests for issue #129 — README + /help coverage of system labels and the
typed translation drill (now the single "Typed drill" `/games` button backed
by `typed_drill.py`; this file was `test_repeat_game_docs.py` before the
repeat_game/focus_drill merge, and the two buttons were later folded into
one drill with salted `remembered` mastery checks).

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. These
are docs-tests — they read README.md and bot.py from disk as text rather than
importing the module, so they assert what users actually see when grepping
the repo for label names and streak rules. The drill runs up to
`typed_drill.MAX_ROUNDS` (10) rounds, so the "10 rounds" doc assertion below
is the module truth.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
README_TEXT = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
BOT_PY_TEXT = (REPO_ROOT / "bot.py").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of `## <heading>` up to the next `## ` heading."""
    pattern = rf"(?ms)^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, text)
    assert match is not None, f"section '## {heading}' not found in README"
    return match.group(1)


def _games_row(text: str) -> str:
    """Return the Commands-table row whose first cell starts with `/games`."""
    # Pipe-tables: a row is one line beginning with `|` and the first cell
    # holds the command.  Match the row regardless of any [<spec>...] suffix.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and re.match(r"\|\s*`?/games\b", stripped):
            return stripped
    raise AssertionError("could not locate the /games row in the Commands table")


# --- AC1: system-managed label naming and semantics ----------------------


def test_readme_labels_section_names_both_tokens_verbatim() -> None:  # AC1(a), edge: verbatim tokens
    section = _section(README_TEXT, "Labels")
    assert "remembered" in section, (
        "Labels section must mention the literal token `remembered` so users "
        "can grep-match what the bot writes"
    )
    assert "focus:hard" in section, (
        "Labels section must mention the literal token `focus:hard` so users "
        "can grep-match what the bot writes"
    )


def test_readme_marks_them_as_system_managed() -> None:  # AC1(b)
    section = _section(README_TEXT, "Labels").lower()
    assert "system" in section, (
        "Labels section must mark the two reserved labels as system-managed"
    )
    # Either phrasing variant — "system-managed" / "system label" / "managed
    # automatically" — counts; what matters is the user-can't-toggle signal.
    user_cant_toggle = (
        "system-managed" in section
        or "system label" in section
        or "managed automatically" in section
    )
    assert user_cant_toggle, (
        "Labels section must state that `remembered` / `focus:hard` are "
        "managed automatically and cannot be toggled by hand"
    )


def test_readme_documents_remembered_semantics() -> None:  # AC1(c) — remembered
    section = _section(README_TEXT, "Labels").lower()
    # graduation tag — auto-attached.
    assert "graduation" in section, (
        "`remembered` semantics must mention graduation"
    )
    # excludes from pushes / quizzes.
    assert "push" in section, "remembered semantics must reference pushes"
    assert "exclud" in section or "only" in section, (
        "remembered semantics must state it is excluded from the regular pool "
        "(or that it is the only pool the drill's mastery checks draw from)"
    )


def test_readme_documents_focus_hard_semantics() -> None:  # AC1(c) — focus:hard
    section = _section(README_TEXT, "Labels")
    lower = section.lower()
    # 2× selection-weight boost.
    boost_mentioned = (
        "2×" in section
        or "2x" in lower
        or "twice" in lower
        or "double" in lower
    )
    assert boost_mentioned, (
        "`focus:hard` semantics must describe the 2× selection weight"
    )
    # Auto-attached on a missed Typed-drill mastery check.
    assert "mastery check" in lower or "typed drill" in lower or "typed-drill" in lower, (
        "`focus:hard` semantics must reference the Typed drill's mastery "
        "checks as the trigger"
    )
    assert "miss" in lower or "wrong" in lower or "missed" in lower, (
        "`focus:hard` semantics must say it attaches when a mastery check is missed"
    )


# --- AC2: streak rule with four literal values ---------------------------


def test_readme_streak_rule_has_four_literal_values() -> None:  # AC2, edge: literal values
    section = _section(README_TEXT, "Labels")
    # The streak prose must carry the exact four literals; alternative
    # phrasings like "one point" / "half" do not count.
    for literal, label in (
        ("+1", "push success → +1"),
        ("+0.5", "game success → +0.5"),
        ("0", "failure resets to 0"),
        ("3", "threshold = 3 → auto-remembered"),
    ):
        assert literal in section, (
            f"Labels section must contain the literal `{literal}` for the "
            f"streak rule ({label})"
        )
    # And the streak rule must explicitly connect threshold 3 to auto-labelling
    # `remembered`, so readers know what crossing the threshold does.
    lower = section.lower()
    assert "streak" in lower, "Labels section must describe a streak mechanic"
    assert "remembered" in section, (
        "Streak rule must say crossing the threshold auto-labels `remembered`"
    )


# --- AC3: /games row mentions the Typed drill mode ------------------------


def test_readme_games_row_describes_typed_drill_mode() -> None:  # AC3
    row = _games_row(README_TEXT)
    lower = row.lower()
    # All five signals the merged single-button drill demands.
    assert "10 rounds" in lower, (
        f"/games row must state the Typed drill runs up to `10 rounds`; row was:\n{row}"
    )
    assert "typed" in lower, (
        f"/games row must call out a `typed` answer mode; row was:\n{row}"
    )
    assert "random direction" in lower, (
        f"/games row must mention `random direction` per round; row was:\n{row}"
    )
    assert "remembered" in lower, (
        f"/games row must say `remembered` words are salted into the drill "
        f"as mastery checks; row was:\n{row}"
    )
    assert "mastery check" in lower, (
        f"/games row must describe the salted rounds as mastery checks; "
        f"row was:\n{row}"
    )


# --- AC4: bot.py COMMANDS games entry ------------------------------------


def test_bot_py_commands_games_mentions_typed_drill_and_remembered() -> None:  # AC4
    # Find the COMMANDS entry whose key is exactly "games".
    match = re.search(r'\(\s*"games"\s*,\s*"([^"]*)"\s*\)', BOT_PY_TEXT)
    assert match is not None, (
        "bot.py must contain a COMMANDS tuple keyed by \"games\""
    )
    desc = match.group(1)
    assert "Typed drill" in desc, (
        f"COMMANDS[\"games\"] description must mention `Typed drill`; got: {desc!r}"
    )
    assert "remembered" in desc, (
        f"COMMANDS[\"games\"] description must mention `remembered`; got: {desc!r}"
    )


# --- invariants ----------------------------------------------------------


def test_readme_preserves_existing_csv_and_focus_prose() -> None:  # invariant — no other section touched
    """The docs change must not regress pre-existing label-surface prose:
    spec syntax, the four label commands, the `/focus` slash command, the CSV
    `labels` column, and the `;` separator must all still appear in the
    Labels section.  Cheap canary against an accidental rewrite of unrelated
    paragraphs.
    """
    section = _section(README_TEXT, "Labels")
    for marker in ("/label", "/unlabel", "/labels", "/focus", "key:value", "pos:"):
        assert marker in section, (
            f"Labels section regressed: pre-existing marker {marker!r} is missing"
        )
    # CSV labels column + `;` separator live in some `/import` or `/export`
    # section that was not supposed to be touched by this change.
    assert "labels" in README_TEXT and ";" in README_TEXT, (
        "README must still reference the CSV `labels` column and `;` separator"
    )
