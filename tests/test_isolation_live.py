"""Real restriction tests against the container worker.

PASS means the restriction held on the actual backend. When the runtime is
missing these are BLOCKED skips — never silent host execution, never passes.
Pure argv/flag tests below need no daemon and always run.
"""
import os
import subprocess

from local_coder.execution.isolated import (
    build_docker_argv,
    translate_argv,
)

from .conftest import fixture_paths, make_controller


def _names():
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                         capture_output=True, text=True, timeout=30)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def test_restriction_flags_are_structural():
    argv = build_docker_argv("img:1", "lc-x", "/tmp/cid", "/tmp/cand", "/tmp/rep",
                             501, 20, ["python3", "-m", "pytest"])
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--pull") + 1] == "never"
    assert "--memory" in argv and "--cpus" in argv and "--pids-limit" in argv
    mounts = [argv[i + 1] for i, t in enumerate(argv) if t == "-v"]
    assert len(mounts) == 2, mounts
    assert all(m.startswith(("/tmp/cand:", "/tmp/rep:")) for m in mounts)
    assert not any("HOME" in t and "/Users" in t or "/root" in t for t in argv)
    envs = [argv[i + 1] for i, t in enumerate(argv) if t == "--env"]
    assert not any("=" in e and len(e.split("=", 1)[1]) > 64 for e in envs), envs


def test_argv_translation_is_controller_owned(tmp_path):
    import sys

    host_report = str(tmp_path / "r.xml")
    out = translate_argv([sys.executable, "-m", "pytest", "tests/",
                          "--junitxml", host_report, "--rootdir", "/host/cand"])

    assert out[0] == "python3"
    assert out[out.index("--junitxml") + 1] == "/reports/report.xml"
    assert out[out.index("--rootdir") + 1] == "/work"
    assert host_report not in out and "/host/cand" not in out


def test_container_has_no_network(isolated_backend, tmp_path):
    code = ("import socket;"
            "socket.create_connection(('8.8.8.8', 53), timeout=5);"
            "print('NETWORK-OPEN')")
    result = isolated_backend.run(["python3", "-c", code], tmp_path, 60, 65536)
    assert "NETWORK-OPEN" not in result.log_text
    assert result.exit_code != 0


def test_container_sees_no_host_secrets_or_home(isolated_backend, tmp_path):
    marker = tmp_path / "host-marker.txt"
    marker.write_text("host-only\n")
    os.environ["LOCAL_CODER_PROBE_SECRET"] = "s3cr3t-probe-value"
    try:
        code = ("import os;"
                f"marker = os.path.exists({str(marker)!r});"
                "secret = os.environ.get('LOCAL_CODER_PROBE_SECRET');"
                "home = os.environ.get('HOME');"
                "print('CLEAN' if not marker and secret is None and home == '/tmp' else 'LEAKED')")
        result = isolated_backend.run(["python3", "-c", code], tmp_path, 60, 65536)
    finally:
        del os.environ["LOCAL_CODER_PROBE_SECRET"]
    assert "CLEAN" in result.log_text, result.log_text[-500:]
    assert result.exit_code == 0


def test_timeout_kills_container_without_leftovers(isolated_backend, tmp_path):
    before = {n for n in _names() if n.startswith("lc-")}
    result = isolated_backend.run(
        ["python3", "-c", "import time; time.sleep(120)"], tmp_path, 5, 65536)
    assert result.timed_out is True
    after = {n for n in _names() if n.startswith("lc-")}
    assert after <= before, f"leftover containers: {after - before}"


def test_full_episode_solves_on_isolated_backend(tmp_path, state_dir, isolated_backend):
    import json
    import shutil

    paths = fixture_paths("demo_calc")
    repo = tmp_path / "repo"
    shutil.copytree(paths["repo"], repo)
    gold = (paths["repo"].parent / "gold.diff").read_text()
    script = [
        '{"action":"find","query":"add subtracts calc","symbols":["add"]}',
        json.dumps({"action": "patch", "operation": "apply", "diff": gold,
                    "expected_effect": "add() returns the sum"}),
        '{"action":"check","scope":"focused"}',
        '{"action":"done","status":"solved","summary":"Fixed add()."}',
    ]
    controller, _ = make_controller(repo, state_dir, script, ticket_file=paths["ticket"],
                                    backend=isolated_backend, episode_id="ep-isolated")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.final is not None and state.final.status == "pass"
