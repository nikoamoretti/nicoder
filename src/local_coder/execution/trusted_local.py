"""Trusted-local backend: explicit, labeled, NOT a sandbox.

Runs candidate tests as the invoking user on this host. Suitable for the
user's own trusted repositories and development fixtures. Must be selected
explicitly and is always labeled non-sandboxed in CLI output and records.
Never used as a silent fallback when isolation is requested but missing.
"""
from __future__ import annotations

from pathlib import Path

from .base import ExecutionBackend, RunResult, run_host_command


class TrustedLocalBackend(ExecutionBackend):
    """Non-sandboxed host execution for explicitly trusted fixtures."""

    name = "trusted-local"
    sandboxed = False
    isolation_label = "NON-SANDBOXED host execution as invoking user (trusted fixtures only)"

    def run(self, argv: list[str], cwd: Path, timeout_s: int, max_log_bytes: int) -> RunResult:
        """Execute the registered command on the host with limits."""
        return run_host_command(argv, cwd, timeout_s, max_log_bytes)
