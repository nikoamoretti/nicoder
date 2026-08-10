"""Shared helpers for deterministic scorers."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_json_text(text: str | None) -> tuple[Any | None, str | None]:
    """Parse ``text`` as strict JSON.

    Returns ``(value, error)``. Only exactly-valid JSON parses; anything else
    is reported with an error string. No fuzziness, no code-fence stripping for
    scoring purposes (a fenced block is treated as prose).
    """
    if text is None or not text.strip():
        return None, "empty or null response text"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"


def count_json_prose_segments(text: str | None) -> int:
    """Number of JSON documents embedded in the text (0 when unparseable)."""
    if not text:
        return 0
    stripped = _FENCE_RE.sub("", text.strip())
    # Split on possible boundaries between adjacent JSON docs.
    segments = [s for s in stripped.split("}{") if s.strip()]
    count = 0
    for seg in segments:
        try:
            json.loads(seg)
            count += 1
        except json.JSONDecodeError:
            pass
    return count
