"""A preview that is replaced or discarded stops at once: a dev server that has gone is not waited for as if it ran.

The helper starts the dev server as its own child. Once the server exits it stays in the process table as a zombie until
its parent collects it, and `kill(pid, 0)` finds a zombie, so `stop_preview` used to wait out its whole 5 s deadline
for a server that was already gone: every second preview of a project, and every Discard, started 5 s late.
"""
from __future__ import annotations

import os
import subprocess
import time

from local_coder.general import preview as preview_module


def _state(pid: int) -> str:
    ps = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return ps.stdout.strip()


def test_a_child_that_has_exited_is_not_alive_even_before_it_is_collected():
    child = subprocess.Popen(["true"])
    deadline = time.monotonic() + 5
    while not _state(child.pid).startswith("Z") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _state(child.pid).startswith("Z"), "it has exited and nobody has collected it: a zombie"
    os.kill(child.pid, 0)  # still in the process table: what fooled kill(pid, 0)
    assert preview_module._alive(child.pid) is False
    assert preview_module._alive(child.pid) is False, "and it stays gone once collected"


def test_a_running_child_a_stranger_and_odd_pids():
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert preview_module._alive(child.pid) is True
        assert preview_module._alive(os.getppid()) is True, "not our child: asked of the process table"
        assert preview_module._alive(0) is False and preview_module._alive(-1) is False
        assert preview_module._alive(2_000_000) is False, "no such process"
    finally:
        child.kill()
        child.wait()


def test_stop_preview_does_not_wait_for_a_dev_server_that_is_already_gone():
    server = subprocess.Popen(["sleep", "300"], start_new_session=True)
    preview_module.write_preview_record({"ok": True, "pid": server.pid, "url": "http://127.0.0.1:1/", "dir": "/x",
                                         "project_id": "x"})
    try:
        started = time.monotonic()
        result = preview_module.stop_preview()
        elapsed = time.monotonic() - started
        assert result == {"stopped": True, "pid": server.pid}
        assert elapsed < 2.0, f"took {elapsed:.1f} s: the dead server was waited for as if it ran"
        assert not preview_module._alive(server.pid)
        assert preview_module.read_preview() is None, "the record goes with it"
    finally:
        server.kill()
        server.wait()
