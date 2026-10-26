"""Marker files while the engine works, so an install never quits the app in the middle of a run.

Each change or question in flight holds `<data root>/busy/<pid>-<kind>-<thread>.json`; the file goes when the work
ends. An installer treats a marker whose process is alive as busy (nicoder-team `install-when-closed.sh`). Nico,
2026-10-26: an install quit the app during a long model reply and stopped his request at step 77.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from local_coder.engines.opencode_config import data_root


def busy_dir() -> Path:
    return data_root() / "busy"


@contextmanager
def working(kind: str) -> Iterator[None]:
    """Hold a marker file while `kind` work ("run" or "question") runs; a marker that cannot be written is skipped."""
    path = busy_dir() / f"{os.getpid()}-{kind}-{threading.get_ident()}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "kind": kind, "since": time.time()}) + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
