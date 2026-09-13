"""Execution safety: no silent fallback, honest BLOCKED, accurate labels."""
import subprocess
import sys
from pathlib import Path

import pytest

from local_coder.execution.isolated import IsolatedBackend, IsolationUnavailable

from .conftest import FIXTURE_DIR


def test_isolated_unavailable_is_blocked_not_pass():
    backend = IsolatedBackend()
    try:
        backend.check_available()
    except IsolationUnavailable as exc:
        pytest.skip(f"BLOCKED: {exc}")
    # If a runtime+image ever exists, M1 still refuses honestly (not wired).
    with pytest.raises(IsolationUnavailable):
        backend.run(["echo", "hi"], Path("/tmp"), 10, 1024)


def test_cli_refuses_isolated_without_fallback(tmp_path):
    import os

    repo = tmp_path / "repo"
    import shutil

    from .conftest import FIXTURE_REPO

    shutil.copytree(FIXTURE_REPO, repo)
    state_dir = tmp_path / "ep"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    run = subprocess.run(
        [sys.executable, "-m", "local_coder.cli", "--state-dir", str(state_dir),
         "run", "--repo", str(repo), "--ticket-file", str(FIXTURE_DIR / "ticket.md"),
         "--profile", "fake:success", "--execution", "isolated",
         "--episode-id", "ep-nofallback"],
        capture_output=True, text=True, env=env, timeout=120)
    assert run.returncode != 0
    assert "fall back" in run.stderr
    assert not (state_dir / "ep-nofallback").exists(), "no episode may start without a backend"


def test_trusted_local_is_labeled_non_sandboxed(repo_copy, state_dir):
    from .conftest import make_controller

    controller, _ = make_controller(repo_copy, state_dir, [])
    assert controller.backend.sandboxed is False
    assert "NON-SANDBOXED" in controller.backend.isolation_label
