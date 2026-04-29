"""Sanity checks for scripts/agent-review.sh.

We don't shell out to a real `gh` or `claude` (those would hit live services
and burn quota). The validation logic — argument parsing and dependency
checks — is testable on its own, and a bash syntax check catches typos.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent-review.sh"


def test_script_passes_bash_syntax_check() -> None:
    bash = shutil.which("bash")
    assert bash, "bash is required for this test"
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT.name} should be executable"


def test_script_rejects_missing_argument() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 1, result.stderr
    assert "usage:" in result.stderr.lower()


def test_script_rejects_non_numeric_argument() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "not-a-number"], capture_output=True, text=True
    )
    assert result.returncode == 1, result.stderr
    assert "usage:" in result.stderr.lower()


def test_script_invokes_review_pr_skill_in_prompt() -> None:
    text = SCRIPT.read_text()
    assert "review-pr skill" in text


def test_script_uses_opus_model() -> None:
    text = SCRIPT.read_text()
    assert "claude-opus-4-7" in text


def test_script_uses_no_session_persistence() -> None:
    text = SCRIPT.read_text()
    assert "--no-session-persistence" in text


def test_script_bypasses_permission_prompts() -> None:
    text = SCRIPT.read_text()
    assert "--permission-mode bypassPermissions" in text


def test_script_opts_into_sandbox_for_root() -> None:
    """claude refuses bypassPermissions under root unless IS_SANDBOX=1
    is set in the environment of the claude invocation."""
    text = SCRIPT.read_text()
    assert "IS_SANDBOX=1 claude -p" in text


def test_script_writes_log_under_logs_agents() -> None:
    text = SCRIPT.read_text()
    assert "logs/agents" in text
    assert "review-" in text


def test_script_validates_in_review_state() -> None:
    """Stage 3 only runs on state:in-review."""
    text = SCRIPT.read_text()
    assert "state:in-review" in text


def test_script_resolves_pr_via_closes_search() -> None:
    """Stage 3 finds the PR by 'Closes #<N>' search before dispatching."""
    text = SCRIPT.read_text()
    assert "gh pr list --search" in text
    assert "Closes #" in text


def test_script_passes_pr_number_to_agent() -> None:
    """The agent prompt must reference both the issue and PR numbers."""
    text = SCRIPT.read_text()
    assert "PR is #" in text or "PR #${pr_number}" in text or "PR is #${pr_number}" in text
