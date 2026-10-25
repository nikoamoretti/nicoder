"""`git_is_dirty` only reads: asking whether a checkout is dirty leaves nothing behind in its `.git` folder."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from local_coder import controller
from local_coder.controller import git_is_dirty


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _inside_git(repo: Path) -> dict[str, bytes | None]:
    """Every entry of `.git` (the index by its bytes), so a new socket, folder or rewritten index shows."""
    git = repo / ".git"
    return {name: (git / name).read_bytes() if name == "index" else None for name in sorted(os.listdir(git))}


def test_a_repository_with_fsmonitor_on_gets_no_daemon_files(repo):
    """With `core.fsmonitor=true`, a bare `git status` starts a daemon and leaves `fsmonitor--daemon*` in `.git`."""
    assert _git(repo, "config", "core.fsmonitor", "true").returncode == 0
    before = _inside_git(repo)
    try:
        assert git_is_dirty(repo) is False
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        assert git_is_dirty(repo) is True
        (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
        assert git_is_dirty(repo) is True
        assert _inside_git(repo) == before, "nothing added to .git, and the index untouched"
        assert "not watching" in _git(repo, "fsmonitor--daemon", "status").stdout, "no daemon was started"
    finally:
        _git(repo, "fsmonitor--daemon", "stop")


def test_the_dirty_check_turns_fsmonitor_off_for_its_one_call(repo, monkeypatch):
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(controller.subprocess, "run", spy)
    git_is_dirty(repo)
    command = next(argv for argv in seen if "status" in argv)
    assert command[:3] == ["git", "-c", "core.fsmonitor=false"], "ahead of the subcommand, where git reads it"
    assert command[-2:] == ["status", "--porcelain"]
