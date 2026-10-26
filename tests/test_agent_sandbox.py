"""The agent's command sandbox on macOS: writes, secrets, Keychain, environment, time limits."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from local_coder.agent.sandbox import build_profile, run_command

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")


@pytest.fixture
def layout(tmp_path):
    home = Path(os.path.realpath(tmp_path / "home"))
    work = home / "work"
    work.mkdir(parents=True)
    (home / "projects").mkdir()
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("SSH-KEY-CONTENT", encoding="utf-8")
    (work / ".env").write_text("TOKEN=ENV-SECRET-VALUE", encoding="utf-8")
    return home, work, tmp_path / "tmp"


def _run(layout, command: str, **kwargs):
    home, work, tmp = layout
    return run_command(command, cwd=work, home=home, writable=[work], tmp=tmp, **kwargs)


def test_profile_blocks_the_keychain_and_key_tool(tmp_path):
    profile = build_profile(home=tmp_path, writable=[tmp_path / "work"])
    assert '(deny process-exec (literal "/usr/bin/security"))' in profile
    assert f'(deny process-exec (literal "{os.path.realpath(tmp_path)}/.local/bin/key"))' in profile
    assert '(deny mach-lookup (global-name "com.apple.SecurityServer"))' in profile


def test_writes_land_only_in_the_working_copy(layout):
    home, work, _ = layout
    assert _run(layout, "echo ok > made.txt").exit_code == 0
    assert (work / "made.txt").read_text() == "ok\n"
    blocked = _run(layout, f"echo x > '{home}/projects/escape.txt'")
    assert blocked.exit_code != 0 and "Operation not permitted" in blocked.output
    assert not (home / "projects" / "escape.txt").exists()


def test_secrets_cannot_be_read(layout):
    home, _, _ = layout
    for command in (f"cat '{home}/.ssh/id_ed25519'", "cat .env"):
        result = _run(layout, command)
        assert result.exit_code != 0
        assert "SSH-KEY-CONTENT" not in result.output and "ENV-SECRET-VALUE" not in result.output


def test_security_cli_is_blocked(layout):
    result = _run(layout, "/usr/bin/security list-keychains")
    assert result.exit_code != 0


def test_environment_has_no_inherited_keys(layout, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-leak-check")
    result = _run(layout, "env")
    assert "nvapi-leak-check" not in result.output
    assert "GIT_TERMINAL_PROMPT=0" in result.output


def test_time_limit_and_stop(layout):
    timed = _run(layout, "sleep 20", timeout_s=1)
    assert timed.timed_out and timed.exit_code is None and timed.seconds < 10
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    stopped = _run(layout, "sleep 20", cancel=cancel)
    assert stopped.cancelled and stopped.seconds < 10


def test_background_jobs_end_with_the_command(layout):
    home, work, _ = layout
    result = _run(layout, "(sleep 1; echo late > late.txt) & echo started")
    assert result.exit_code == 0 and "started" in result.output
    threading.Event().wait(1.5)
    assert not (work / "late.txt").exists()


def test_long_output_is_cut_in_the_middle(layout):
    result = _run(layout, "python3 -c \"print('a' * 50000 + 'TAIL')\"", max_output=2000)
    assert result.truncated
    assert result.output.rstrip().endswith("TAIL")
    assert "characters cut" in result.output


def test_a_command_can_stop_its_own_processes_but_not_others(layout):
    import subprocess

    outside = subprocess.Popen(["sleep", "30"])
    try:
        result = _run(layout, f"sleep 30 & kid=$!; kill $kid; echo own=$?; kill {outside.pid}; echo outside=$?")
        assert "own=0" in result.output
        assert "outside=1" in result.output
        assert outside.poll() is None  # still running
    finally:
        outside.kill()
        outside.wait()
