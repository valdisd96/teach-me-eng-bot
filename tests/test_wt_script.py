"""Integration tests for scripts/wt.sh.

Each test spins up a tiny git repo in tmp_path and shells out to the script,
so we exercise the real bash logic rather than mocking it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wt.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "wt-test@example.com")
    _git(path, "config", "user.name", "wt-test")
    (path / ".env.example").write_text("TELEGRAM_TOKEN=placeholder\n")
    (path / ".gitignore").write_text(
        ".env\n.venv\nworktrees/\nenv/slot*.env\n"
    )
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    # .venv lives next to the repo but is never tracked, mirroring the real
    # project. Creating it after the commit keeps it out of worktree checkouts.
    (path / ".venv").mkdir()
    (path / ".venv" / "marker").write_text("real venv\n")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo")


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    r = _init_repo(tmp_path / "repo")
    _git(r, "remote", "add", "origin", str(bare))
    _git(r, "push", "-u", "origin", "main")
    return r


def run_wt(
    cwd: Path,
    *args: str,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(WT_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        input=stdin,
        check=check,
    )


# create -----------------------------------------------------------------


def test_create_basic_layout(repo: Path) -> None:
    run_wt(repo, "create", "x")
    wt = repo / "worktrees" / "x"
    assert wt.is_dir()
    assert (wt / "data").is_dir()
    assert (wt / ".venv").is_symlink()
    assert (wt / ".venv").resolve() == (repo / ".venv").resolve()
    assert (wt / ".env").is_file()
    assert (wt / ".env").read_text() == (repo / ".env.example").read_text()


def test_create_records_slot_in_gitdir(repo: Path) -> None:
    run_wt(repo, "create", "x")
    gitdir_pointer = (repo / "worktrees" / "x" / ".git").read_text().strip()
    gitdir = Path(gitdir_pointer.removeprefix("gitdir: "))
    assert (gitdir / "wt-slot").read_text().strip() == "1"


def test_create_auto_picks_next_free_slot(repo: Path) -> None:
    run_wt(repo, "create", "a")
    run_wt(repo, "create", "b")
    listing = run_wt(repo, "list").stdout
    assert "slot=1" in listing
    assert "slot=2" in listing


def test_create_explicit_slot(repo: Path) -> None:
    run_wt(repo, "create", "a", "2")
    listing = run_wt(repo, "list").stdout
    assert "slot=2" in listing
    # slot=1 is the main worktree's "-" placeholder, not a real slot
    assert "\tslot=1" not in listing


def test_create_rejects_when_all_slots_taken(repo: Path) -> None:
    run_wt(repo, "create", "a")
    run_wt(repo, "create", "b")
    result = run_wt(repo, "create", "c", check=False)
    assert result.returncode != 0
    assert "no free slot" in result.stderr


def test_create_rejects_taken_explicit_slot(repo: Path) -> None:
    run_wt(repo, "create", "a", "1")
    result = run_wt(repo, "create", "b", "1", check=False)
    assert result.returncode != 0
    assert "in use" in result.stderr


def test_create_rejects_out_of_range_slot(repo: Path) -> None:
    result = run_wt(repo, "create", "a", "9", check=False)
    assert result.returncode != 0
    assert "1..2" in result.stderr


def test_create_rejects_existing_worktree(repo: Path) -> None:
    run_wt(repo, "create", "x")
    result = run_wt(repo, "create", "x", check=False)
    assert result.returncode != 0


def test_create_seeds_slot_env_from_example(repo: Path) -> None:
    assert not (repo / "env" / "slot1.env").exists()
    run_wt(repo, "create", "x")
    seeded = repo / "env" / "slot1.env"
    assert seeded.is_file()
    assert seeded.read_text() == (repo / ".env.example").read_text()


def test_create_keeps_existing_slot_env(repo: Path) -> None:
    (repo / "env").mkdir()
    (repo / "env" / "slot1.env").write_text("TELEGRAM_TOKEN=real-token\n")
    run_wt(repo, "create", "x")
    wt_env = (repo / "worktrees" / "x" / ".env").read_text()
    assert "real-token" in wt_env


def test_create_nested_branch_symlink_depth(repo: Path) -> None:
    run_wt(repo, "create", "feat/abc")
    sym = repo / "worktrees" / "feat" / "abc" / ".venv"
    assert sym.is_symlink()
    assert os.readlink(sym) == "../../../.venv"
    assert sym.resolve() == (repo / ".venv").resolve()


def test_create_simple_branch_symlink_depth(repo: Path) -> None:
    run_wt(repo, "create", "x")
    sym = repo / "worktrees" / "x" / ".venv"
    assert os.readlink(sym) == "../../.venv"


# destroy ----------------------------------------------------------------


def test_destroy_clean_worktree(repo: Path) -> None:
    run_wt(repo, "create", "x")
    run_wt(repo, "destroy", "x")
    assert not (repo / "worktrees" / "x").exists()


def test_destroy_aborts_on_dirty_when_user_says_no(repo: Path) -> None:
    run_wt(repo, "create", "x")
    (repo / "worktrees" / "x" / "scratch.txt").write_text("dirty\n")
    result = run_wt(repo, "destroy", "x", stdin="n\n", check=False)
    assert result.returncode != 0
    assert (repo / "worktrees" / "x").exists()


def test_destroy_proceeds_on_dirty_when_user_says_yes(repo: Path) -> None:
    run_wt(repo, "create", "x")
    (repo / "worktrees" / "x" / "scratch.txt").write_text("dirty\n")
    run_wt(repo, "destroy", "x", stdin="y\n")
    assert not (repo / "worktrees" / "x").exists()


def test_destroy_aborts_on_unpushed_commits(repo_with_remote: Path) -> None:
    r = repo_with_remote
    run_wt(r, "create", "x")
    wt = r / "worktrees" / "x"
    _git(wt, "push", "-u", "origin", "x")
    (wt / "newfile.txt").write_text("new\n")
    _git(wt, "add", "newfile.txt")
    _git(wt, "commit", "-m", "wip")
    result = run_wt(r, "destroy", "x", stdin="n\n", check=False)
    assert result.returncode != 0
    assert "unpushed" in result.stderr.lower()
    assert wt.exists()


def test_destroy_missing_worktree(repo: Path) -> None:
    result = run_wt(repo, "destroy", "nope", check=False)
    assert result.returncode != 0


# list -------------------------------------------------------------------


def test_list_includes_main_worktree_with_dash_slot(repo: Path) -> None:
    listing = run_wt(repo, "list").stdout
    assert str(repo) in listing
    assert "slot=-" in listing


def test_list_shows_slot_per_worktree(repo: Path) -> None:
    run_wt(repo, "create", "a", "1")
    run_wt(repo, "create", "b", "2")
    listing = run_wt(repo, "list").stdout
    assert "slot=1" in listing
    assert "slot=2" in listing


# misc -------------------------------------------------------------------


def test_unknown_command_errors(repo: Path) -> None:
    result = run_wt(repo, "frobnicate", check=False)
    assert result.returncode != 0
    assert "unknown command" in result.stderr


def test_no_args_prints_usage(repo: Path) -> None:
    result = run_wt(repo, check=False)
    assert result.returncode != 0
    assert "Usage:" in result.stderr
