"""Isolated backend: real container worker with stated restrictions.

Each CHECK runs inside `python:3.12-slim` with `--network none`, a fixed
non-root user, memory/CPU/pids limits, `--pull never`, and exactly two
bind mounts: the candidate tree at /work and a scratch reports dir at
/reports. No home directory, no credentials, no host environment, and no
model-server socket are mounted or passed. Timeouts kill the container;
leftover containers are removed best-effort.

A Git worktree or directory copy is not an OS security sandbox; only this
backend may claim process/network restriction, and only these actual
restrictions, verified by tests against the real backend. When the runtime
or image is missing, every call raises IsolationUnavailable (BLOCKED) and
the CLI refuses instead of silently running on the host.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .base import ExecutionBackend, RunResult

REPORTS_MOUNT = "/reports"
WORK_MOUNT = "/work"
REPORT_NAME = "report.xml"


class IsolationUnavailable(Exception):
    """Isolation was requested but the backend cannot run. Marked BLOCKED."""


def build_docker_argv(
    image: str,
    name: str,
    cidfile: str,
    candidate: str,
    reports: str,
    uid: int,
    gid: int,
    command: list[str],
) -> list[str]:
    """Pure constructor for the container invocation (unit-testable).

    Restrictions asserted here: no network, never pull, fixed user, memory /
    CPU / pids caps, init reaping, read-only root is NOT used (pytest needs
    to write pycache-free temp state? actually PYTHONDONTWRITEBYTECODE covers
    it; /work stays rw so detected test-source mutations remain visible),
    and exactly the two approved mounts.
    """
    return [
        "docker", "run", "--rm",
        "--pull", "never",
        "--network", "none",
        "--name", name,
        "--cidfile", cidfile,
        "--user", f"{uid}:{gid}",
        "--memory", "2g",
        "--cpus", "2",
        "--pids-limit", "256",
        "--init",
        "-v", f"{candidate}:{WORK_MOUNT}:rw",
        "-v", f"{reports}:{REPORTS_MOUNT}:rw",
        "-w", WORK_MOUNT,
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
        "--env", "HOME=/tmp",
        "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=",
        image,
        *command,
    ]


def translate_argv(argv: list[str]) -> list[str]:
    """Rewrite host-shaped argv for the container (controller-owned).

    argv[0] may be a host interpreter path (tests pin sys.executable); inside
    the worker it becomes `python3`. Host --junitxml/--rootdir paths become
    the /reports mount and /work. Anything else passes through unchanged;
    the model never influences argv.
    """
    out: list[str] = []
    skip_next_rootdir = False
    for index, token in enumerate(argv):
        if skip_next_rootdir:
            skip_next_rootdir = False
            continue
        if index == 0 and (token.endswith("/python3") or token.endswith("/python")
                           or token in ("python3", "python")):
            out.append("python3")
        elif token == "--junitxml":
            out.append(token)
        elif index > 0 and argv[index - 1] == "--junitxml":
            out.append(f"{REPORTS_MOUNT}/{REPORT_NAME}")
        elif token == "--rootdir":
            skip_next_rootdir = True
            out.extend([token, WORK_MOUNT])
        else:
            out.append(token)
    return out


class IsolatedBackend(ExecutionBackend):
    """Container-backed execution with enforced restrictions."""

    name = "isolated"
    sandboxed = True
    isolation_label = "container worker (--network none, user-namespace uid, mem/cpu/pids caps)"
    default_image = "local-coder-worker:1"

    def __init__(self, image: str | None = None) -> None:
        import os as _os

        self.image = image or _os.environ.get("LOCAL_CODER_WORKER_IMAGE", self.default_image)

    def check_available(self) -> None:
        """Raise IsolationUnavailable unless docker and the image exist."""
        if shutil.which("docker") is None:
            raise IsolationUnavailable("docker runtime not found; isolation BLOCKED (no silent host fallback)")
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=15)
        if info.returncode != 0:
            raise IsolationUnavailable(f"docker daemon unreachable; isolation BLOCKED: {info.stderr.strip()[:200]}")
        inspect = subprocess.run(
            ["docker", "image", "inspect", self.image], capture_output=True, text=True, timeout=15
        )
        if inspect.returncode != 0:
            raise IsolationUnavailable(
                f"image {self.image} not present; isolation BLOCKED "
                f"(build it explicitly: docker build -t {self.image} "
                f"-f docker/worker.Dockerfile docker/; refusing to auto-build)"
            )

    def run(self, argv: list[str], cwd: Path, timeout_s: int, max_log_bytes: int) -> RunResult:
        """Execute one trusted command inside the container worker."""
        self.check_available()
        reports = Path(tempfile.mkdtemp(prefix="lc-reports-"))
        name = f"lc-{uuid.uuid4().hex[:12]}"
        cidfile = reports / "cid"
        container_argv = translate_argv(argv)
        docker_argv = build_docker_argv(
            self.image, name, str(cidfile), str(cwd), str(reports),
            os.getuid(), os.getgid(), container_argv)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                docker_argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except OSError as exc:
            shutil.rmtree(reports, ignore_errors=True)
            return RunResult(list(argv), None, False, 0.0,
                             f"<could not start container: {exc}>\n", False, container=None)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_container(cidfile, name)
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            out, _ = proc.communicate()
        duration = time.monotonic() - started
        self._remove_container(cidfile, name)
        produced = reports / REPORT_NAME
        # Hand the JUnit report back where execute_scope expects it.
        for index, token in enumerate(argv):
            if token == "--junitxml" and index + 1 < len(argv) and produced.exists():
                try:
                    shutil.copyfile(produced, argv[index + 1])
                except OSError:
                    pass
        shutil.rmtree(reports, ignore_errors=True)
        out = out or ""
        truncated = len(out.encode("utf-8", "replace")) > max_log_bytes
        if truncated:
            out = out[: max_log_bytes // 4] + f"\n…[log truncated at {max_log_bytes} bytes]\n"
        return RunResult(list(argv), proc.returncode, timed_out, duration, out, truncated,
                         container=None if timed_out else name)

    @staticmethod
    def _read_cid(cidfile: Path) -> str | None:
        try:
            cid = cidfile.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return cid or None

    def _kill_container(self, cidfile: Path, name: str) -> None:
        cid = self._read_cid(cidfile) or name
        subprocess.run(["docker", "kill", cid], capture_output=True, timeout=15)

    def _remove_container(self, cidfile: Path, name: str) -> None:
        cid = self._read_cid(cidfile) or name
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
