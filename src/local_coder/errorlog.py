"""Raw error text stays out of the app's replies.

Nico sees one plain sentence. The exception text and traceback go to the helper's stderr and to a file
under <data dir>/logs/, because the app keeps no log of the helper's stderr. Nothing here ever raises.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

from local_coder.engines.opencode_config import data_root

UNEXPECTED = "Local Coder hit an unexpected problem and could not finish that. Please try again."
NOT_FOUND = "Local Coder could not find that. It may have been moved or deleted."
LOG_NAME = "helper-errors.log"
MAX_LOG_BYTES = 512_000


def record_error(where: str, error: BaseException | str) -> None:
    """Write the raw text (and the traceback of an exception) to stderr and to logs/helper-errors.log."""
    if isinstance(error, BaseException):
        body = "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
    else:
        body = str(error).rstrip()
    entry = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n{body}\n\n"
    try:
        sys.stderr.write(entry)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    try:
        folder = data_root() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / LOG_NAME
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, folder / f"{LOG_NAME}.1")
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(entry)
    except OSError:
        pass
