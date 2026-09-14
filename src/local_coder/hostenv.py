"""Finder-safe host environment for the Mac app helper.

Launching from Finder often yields a PATH without Homebrew, Docker Desktop,
or the durable local-coder install. The helper adds those well-known
absolute locations itself; users never set PYTHONPATH or activate a venv.
"""
from __future__ import annotations

import os
from pathlib import Path

EXTRA_PATHS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/Applications/Docker.app/Contents/Resources/bin",
    str(Path.home() / ".local" / "bin"),
)

DEFAULT_PYTHON = Path("/usr/local/bin/python3")
DURABLE_VENV = Path.home() / ".local" / "share" / "local-coder-venv"


def apply_host_path() -> str:
    """Prepend known Mac toolchain directories when they exist."""
    parts = [p for p in os.environ.get("PATH", "").split(":") if p]
    for extra in reversed(EXTRA_PATHS):
        if Path(extra).exists() and extra not in parts:
            parts.insert(0, extra)
    os.environ["PATH"] = ":".join(parts)
    return os.environ["PATH"]


def durable_python() -> Path | None:
    """Interpreter that should run the installed engine from the app."""
    candidate = DURABLE_VENV / "bin" / "python"
    if candidate.exists():
        return candidate
    if DEFAULT_PYTHON.exists():
        return DEFAULT_PYTHON
    return None
