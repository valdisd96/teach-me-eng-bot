"""Sanity checks for scripts/agent-plan-exec.sh.

We don't shell out to a real `gh` or `claude` (those would hit live services
and burn quota). The validation logic — argument parsing and dependency
checks — is testable on its own, and a bash syntax check catches typos.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent-plan-exec.sh"


def test_script_passes_bash_syntax_check() -> None:
    bash = shutil.which("bash")
    assert bash, "bash is required for this test"
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_script_is_executable() -> None:
    import os

    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT.name} should be executable"


def test_script_rejects_missing_argument() -> None:
    """No issue number → exit 1, helpful usage line on stderr."""
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


def test_script_invokes_plan_exec_skill_in_prompt() -> None:
    """The dispatched prompt must reference the skill by name so it loads."""
    text = SCRIPT.read_text()
    assert "plan-exec skill" in text


def test_script_uses_opus_model() -> None:
    """Per workflow.md the agents always run on Opus 4.7."""
    text = SCRIPT.read_text()
    assert "claude-opus-4-7" in text


def test_script_uses_no_session_persistence() -> None:
    """Each Stage 1 dispatch must be a fresh session."""
    text = SCRIPT.read_text()
    assert "--no-session-persistence" in text


def test_script_bypasses_permission_prompts() -> None:
    """Headless mode has no human to approve tool calls."""
    text = SCRIPT.read_text()
    assert "--permission-mode bypassPermissions" in text


def test_script_writes_log_under_logs_agents() -> None:
    """Log location convention used by the orchestrator."""
    text = SCRIPT.read_text()
    assert "logs/agents" in text
    assert "plan-exec-" in text


def test_script_accepts_unlabelled_fresh_issues() -> None:
    """Fresh issues (no state:* label) are valid Stage 1 input — the agent
    applies state:needs-planning itself as part of the initial flip."""
    text = SCRIPT.read_text()
    # The case statement must include "" as a valid starting state.
    assert '""|state:needs-planning|state:needs-rework' in text
