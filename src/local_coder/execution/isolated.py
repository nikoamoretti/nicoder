"""Isolated backend probe (Milestone 1).

Prefers a container worker with no external network behind an installed
container runtime. In Milestone 1 this module probes availability and
refuses honestly when the runtime or image is missing: tests assert the
BLOCKED behavior instead of silently running on the host.

A Git worktree or directory copy is not an OS security sandbox; only this
backend (when available) may claim process/network restriction, and only
its actual restrictions, verified by tests against the real backend.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import ExecutionBackend, RunResult


class IsolationUnavailable(Exception):
    """Isolation was requested but the backend cannot run. Marked BLOCKED."""


class IsolatedBackend(ExecutionBackend):
    """Container-backed execution. Milestone 1: availability probe."""

    name = "isolated"
    sandboxed = True
    isolation_label = "container worker (no network), candidate files only"
    image = "python:3.12-slim"

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
                f"(run: docker pull {self.image}; refusing to auto-download)"
            )

    def run(self, argv: list[str], cwd: Path, timeout_s: int, max_log_bytes: int) -> RunResult:
        """Milestone 1 refuses unless the container path is fully available."""
        self.check_available()
        raise IsolationUnavailable(
            "container execution path is not wired in Milestone 1; "
            "use --execution trusted-local explicitly for trusted fixtures"
        )
