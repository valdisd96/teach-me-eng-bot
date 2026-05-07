"""Tests for issue #88 — README + /help + /start onboarding for labels.

Spec lives in the latest `<!-- agent-plan v1 -->` comment on the issue. This
suite asserts the runtime-visible doc strings (HELP_TEXT, DONE_MESSAGE) and
the README sections that describe the label surface introduced in #82–#87.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")

import bot  # noqa: E402  (env var must be set first)
import config_flow  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
README_TEXT = (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the slice of `text` under a `## <heading>` line up to the next `## `."""
    pattern = rf"(?ms)^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, text)
    assert match is not None, f"section '## {heading}' not found in README"
    return match.group(1)


def _split_sections(text: str) -> list[str]:
    """Yield each `## ...` block (heading included) so we can scan section-locally."""
    parts = re.split(r"(?m)^(?=##\s)", text)
    return [p for p in parts if p.strip()]


# --- README ----------------------------------------------------------------


def test_readme_has_labels_section_heading() -> None:  # AC1 — "## Labels" exists
    assert re.search(r"(?m)^##\s+Labels\s*$", README_TEXT) is not None, (
        "README is missing a top-level '## Labels' section heading"
    )


def test_readme_labels_section_documents_convention() -> None:  # AC1(a) — key:value/bare with example
    section = _section(README_TEXT, "Labels")
    assert "key:value" in section, "Labels section should describe the key:value convention"
    # At least one concrete `key:value` example token should appear (e.g. pos:noun).
    assert re.search(r"\b[a-z]+:[a-z]+\b", section) is not None, (
        "Labels section should include at least one concrete key:value example"
    )


def test_readme_labels_section_documents_four_commands() -> None:  # AC1(b) — four commands listed
    section = _section(README_TEXT, "Labels")
    for cmd in ("/label", "/unlabel", "/labels", "/focus"):
        assert cmd in section, f"Labels section should document {cmd}"


def test_readme_labels_section_documents_and_filtering() -> None:  # AC1(c) — AND across /list, /games, /focus
    section = _section(README_TEXT, "Labels").lower()
    assert "and" in section, "Labels section should describe AND-across-tokens filtering"
    for cmd in ("/list", "/games", "/focus"):
        assert cmd in section, (
            f"Labels section should mention {cmd} as accepting the spec syntax"
        )


def test_readme_labels_section_documents_pos_uniqueness() -> None:  # AC1(d) — pos:* uniqueness rule
    section = _section(README_TEXT, "Labels")
    assert "pos:" in section, "Labels section should reference the pos:* family"
    # Some phrasing about a single / one pos:* per word.
    assert re.search(r"(?i)\bone\b.*\bpos:", section) or re.search(
        r"(?i)\bsingle\b.*\bpos:", section
    ) or re.search(r"(?i)pos:.*\b(only|single|one)\b", section), (
        "Labels section should document the 'one pos:* per word' rule"
    )


def test_readme_csv_section_mentions_labels_column_and_separator() -> None:  # AC4
    """Some heading-bounded section that documents /import or /export must
    mention both the literal CSV column name `labels` and the literal `;`
    separator.
    """
    matching = [
        s
        for s in _split_sections(README_TEXT)
        if ("/import" in s or "/export" in s) and "labels" in s and ";" in s
    ]
    assert matching, (
        "Expected at least one README section that documents /import or /export "
        "to mention both the `labels` CSV column and the `;` separator; found none."
    )


# --- bot.HELP_TEXT ---------------------------------------------------------


def test_help_text_lists_four_label_commands_with_descs() -> None:  # AC2 — em-dash + non-empty desc
    """Every label command must appear as `/<name> — <non-empty>` in HELP_TEXT."""
    for cmd in ("label", "unlabel", "labels", "focus"):
        match = re.search(rf"/{cmd} — (\S[^\n]*)", bot.HELP_TEXT)
        assert match is not None, (
            f"/{cmd} must appear in HELP_TEXT followed by ' — <description>'"
        )
        assert match.group(1).strip(), f"/{cmd} description in HELP_TEXT must be non-empty"


def test_help_text_links_to_readme_labels_anchor() -> None:  # AC2 — exact #labels anchor URL
    assert "github.com/valdisd96/teach-me-eng-bot#labels" in bot.HELP_TEXT, (
        "HELP_TEXT must link to the README Labels section via the #labels anchor "
        "(GitHub auto-generates lower-cased anchors from headings)"
    )


# --- config_flow.DONE_MESSAGE ----------------------------------------------


def test_done_message_mentions_label_command() -> None:  # AC3 — contains "/label"
    assert "/label" in config_flow.DONE_MESSAGE, (
        "DONE_MESSAGE must mention /label so users discover labels right after /start"
    )


def test_done_message_under_400_chars() -> None:  # AC3 — length cap
    n = len(config_flow.DONE_MESSAGE)
    assert n <= 400, f"DONE_MESSAGE must stay ≤ 400 chars (was {n})"


# --- invariants ------------------------------------------------------------


def test_commands_label_tuples_unchanged() -> None:  # invariant — COMMANDS still has the 4 label tuples
    """COMMANDS must still expose exactly the four label commands, each once,
    with a non-empty description. The spec forbids adding/removing label
    commands as part of this docs-only change.
    """
    label_cmds = {"label", "unlabel", "labels", "focus"}
    found = [(name, desc) for name, desc in bot.COMMANDS if name in label_cmds]
    names = [name for name, _ in found]
    for cmd in label_cmds:
        assert names.count(cmd) == 1, (
            f"COMMANDS must contain /{cmd} exactly once (got {names.count(cmd)})"
        )
    for name, desc in found:
        assert desc.strip(), f"COMMANDS entry /{name} must have a non-empty description"


def test_done_message_has_no_fstring_substitutions() -> None:  # invariant — no { } substitutions
    """DONE_MESSAGE must remain a single string literal — no f-string
    substitutions added. Easiest observable: it contains no `{` or `}`.
    """
    assert "{" not in config_flow.DONE_MESSAGE, (
        "DONE_MESSAGE must not contain '{' — should remain a static string literal"
    )
    assert "}" not in config_flow.DONE_MESSAGE, (
        "DONE_MESSAGE must not contain '}' — should remain a static string literal"
    )
