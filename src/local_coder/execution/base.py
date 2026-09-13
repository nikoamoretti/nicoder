"""Execution backend interface and shared process handling."""
from __future__ import annotations

import abc
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    """Owned record of one executed test command."""

    argv: list[str]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    log_text: str
    truncated: bool


SECRET_NAME_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def sanitized_env() -> dict[str, str]:
    """Host environment minus secret-shaped and pytest-override variables.

    Sanitized argv execution is required, but this is not proof of
    isolation: candidate code still runs with host user privileges under
    the trusted-local backend.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(hint in upper for hint in SECRET_NAME_HINTS):
            continue
        if key.startswith("PYTEST_"):
            continue
        env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class ExecutionBackend(abc.ABC):
    """Runs registered test commands. The model never supplies argv."""

    name: str = "base"
    sandboxed: bool = False
    isolation_label: str = "unknown"

    @abc.abstractmethod
    def run(self, argv: list[str], cwd: Path, timeout_s: int, max_log_bytes: int) -> RunResult:
        """Execute one trusted command with a deadline and process cleanup."""


def run_host_command(argv: list[str], cwd: Path, timeout_s: int, max_log_bytes: int) -> RunResult:
    """Run argv on the host with timeout, process-tree kill, log caps."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=sanitized_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return RunResult(list(argv), None, False, 0.0, f"<could not start process: {exc}>\n", False)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        out, _ = proc.communicate()
        timed_out = True
    duration = time.monotonic() - started
    out = out or ""
    truncated = len(out.encode("utf-8", "replace")) > max_log_bytes
    if truncated:
        out = out[: max_log_bytes // 4] + f"\n…[log truncated at {max_log_bytes} bytes]\n"
    return RunResult(list(argv), proc.returncode, timed_out, duration, out, truncated)
