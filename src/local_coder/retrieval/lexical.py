"""Literal identifier and text search over the permitted inventory."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_MATCHES = 200
MAX_LINE_CHARS = 500


@dataclass
class LexHit:
    """One literal match with its location."""

    path: str
    lineno: int
    line: str
    token: str


def tokenize(text: str) -> list[str]:
    """Split query text into literal identifier-ish tokens."""
    parts = re.split(r"[^A-Za-z0-9_]+", text)
    seen: list[str] = []
    for part in parts:
        token = part.strip()
        if len(token) >= 3 and token.lower() not in seen:
            seen.append(token.lower())
    return seen[:20]


def literal_search(root: Path, files: list[str], tokens: list[str]) -> list[LexHit]:
    """Case-insensitive literal search. No shell, no regex injection."""
    hits: list[LexHit] = []
    lowered = [t.lower() for t in tokens if t]
    for rel in files:
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            for token in lowered:
                if token in low:
                    hits.append(LexHit(rel, number, line[:MAX_LINE_CHARS], token))
                    if len(hits) >= MAX_MATCHES:
                        return hits
    return hits
