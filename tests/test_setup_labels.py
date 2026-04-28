"""Sanity checks for scripts/setup-labels.sh.

We don't shell out to `gh` (that would hit live GitHub). The script is a flat
list of `create ...` (and `drop ...`) invocations, so the meaningful contract
is "every label named in workflow.md appears in the create section, and every
deprecated label appears in the drop section". A bash syntax check catches
typos in the dispatcher.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup-labels.sh"

EXPECTED_LABELS = [
    # state
    "state:needs-planning",
    "state:in-progress",
    "state:clarification-needed",
    "state:tests-pending",
    "state:in-review",
    "state:needs-rework",
    "state:blocked",
    # type
    "type:feat",
    "type:fix",
    "type:chore",
    "type:refactor",
    # priority
    "priority:high",
    "priority:medium",
    "priority:low",
    # area
    "area:bot",
    "area:vocab",
    "area:scheduler",
    "area:llm",
    "area:translator",
    "area:config",
    "area:db",
]

DEPRECATED_LABELS = [
    "state:ready-for-parallel-work",
    "state:ready-for-review",
    "touches:bot.py",
]


def test_script_passes_bash_syntax_check() -> None:
    bash = shutil.which("bash")
    assert bash, "bash is required for this test"
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_script_creates_every_expected_label() -> None:
    text = SCRIPT.read_text()
    missing = [lbl for lbl in EXPECTED_LABELS if f'"{lbl}"' not in text]
    assert not missing, f"setup-labels.sh missing: {missing}"


def test_script_uses_force_for_idempotency() -> None:
    """Re-runs must update in place rather than fail on existing labels."""
    text = SCRIPT.read_text()
    assert "--force" in text


@pytest.mark.parametrize("label", EXPECTED_LABELS)
def test_each_label_has_a_color_argument(label: str) -> None:
    """Catches a `create "name"` line that forgot its colour/description tail."""
    text = SCRIPT.read_text()
    pattern = rf'create\s+"{re.escape(label)}"\s+"[0-9a-fA-F]{{6}}"\s+"[^"]+"'
    assert re.search(pattern, text), (
        f"{label} is missing the expected `create \"name\" \"hex\" \"desc\"` triple"
    )


@pytest.mark.parametrize("label", DEPRECATED_LABELS)
def test_deprecated_labels_are_dropped(label: str) -> None:
    """Re-running setup-labels.sh must clean up the old parallel-design labels."""
    text = SCRIPT.read_text()
    assert f'drop "{label}"' in text, (
        f"{label} should be removed via `drop \"{label}\"` so re-runs are idempotent"
    )
