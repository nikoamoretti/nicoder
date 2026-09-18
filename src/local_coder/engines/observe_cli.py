"""Explicit bounded observation retrieval for the coding agent.

Invoked as `local-coder-observe <id> <offset>` from OpenCode's bash tool.
Never prints the entire stored dump.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .observations import ObservationStore


def main(argv: list[str] | None = None) -> int:
    """Print one bounded slice. Exit 2 on usage or missing observation."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        sys.stderr.write("usage: local-coder-observe <observation_id> <offset>\n")
        return 2
    root = os.environ.get("LOCAL_CODER_OBSERVE_ROOT")
    if not root:
        sys.stderr.write("LOCAL_CODER_OBSERVE_ROOT is not set\n")
        return 2
    store = ObservationStore(Path(root))
    try:
        offset = int(args[1])
        view = store.read_slice(args[0], offset=offset)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"observe failed: {exc}\n")
        return 2
    sys.stdout.write(view.as_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
