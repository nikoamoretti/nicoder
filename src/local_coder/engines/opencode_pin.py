"""Pinned OpenCode identity used by Nicoder.

The product talks to this exact local build. A Homebrew or npm upgrade of a
user-installed OpenCode must not silently change the app's engine.
"""
from __future__ import annotations

from pathlib import Path

PINNED_NAME = "opencode"
PINNED_VERSION = "1.18.22"
PINNED_LICENSE = "MIT"
PINNED_SOURCE = "Homebrew formula opencode 1.18.22 (https://opencode.ai)"
PINNED_HOMEPAGE = "https://github.com/anomalyco/opencode"
PINNED_SHA256 = "33684a2aa6aad5386069fe44031f928aaa36cf860bb6e430ed7241ff92f6c8dd"
SYSTEM_BINARY = Path("/opt/homebrew/bin/opencode")


def pinned_binary() -> Path:
    """App-owned copy of the pinned OpenCode executable."""
    return Path.home() / ".local" / "share" / "local-coder" / "bin" / "opencode"


def pin_record() -> dict[str, str]:
    """Sanitized identity for logs and verification docs."""
    return {
        "name": PINNED_NAME,
        "version": PINNED_VERSION,
        "license": PINNED_LICENSE,
        "source": PINNED_SOURCE,
        "homepage": PINNED_HOMEPAGE,
        "sha256": PINNED_SHA256,
    }
