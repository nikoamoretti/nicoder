"""Rank combined retrieval evidence into line-numbered source ranges."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..paths import sha256_file
from .lexical import LexHit
from .python_symbols import Symbol

CONTEXT_LINES = 10
MAX_EXCERPT_CHARS = 2000
MAX_HITS = 12

W_SYMBOL = 5.0
W_LITERAL = 1.0
W_TRACEBACK = 4.0


@dataclass
class SearchHit:
    """One ranked source range with content identity and reasons."""

    path: str
    start: int
    end: int
    excerpt: str
    file_sha: str
    score: float
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _excerpt(root: Path, path: str, start: int, end: int) -> tuple[str, str]:
    lines = (root / path).read_text(encoding="utf-8", errors="strict").splitlines()
    total = len(lines)
    lo = max(1, start - CONTEXT_LINES)
    hi = min(total, end + CONTEXT_LINES)
    numbered = [f"{n}:{lines[n - 1]}" for n in range(lo, hi + 1)]
    text = "\n".join(numbered)
    if len(text) > MAX_EXCERPT_CHARS:
        text = text[:MAX_EXCERPT_CHARS] + "\n…[excerpt truncated]"
    return text, f"{lo}-{hi}"


def rank(
    root: Path,
    lex_hits: list[LexHit],
    symbols: list[Symbol],
    traceback_paths: list[str],
) -> list[SearchHit]:
    """Combine literal, symbol, and traceback evidence deterministically."""
    buckets: dict[tuple[str, int, int], dict] = {}

    def bucket(path: str, start: int, end: int) -> dict:
        key = (path, start, end)
        if key not in buckets:
            buckets[key] = {"score": 0.0, "components": {}, "reasons": []}
        return buckets[key]

    for hit in lex_hits:
        entry = bucket(hit.path, hit.lineno, hit.lineno)
        entry["score"] += W_LITERAL
        entry["components"]["literal"] = entry["components"].get("literal", 0.0) + W_LITERAL
        reason = f"literal match {hit.token!r} at line {hit.lineno}"
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    for symbol in symbols:
        entry = bucket(symbol.path, symbol.start, symbol.end)
        entry["score"] += W_SYMBOL
        entry["components"]["symbol"] = entry["components"].get("symbol", 0.0) + W_SYMBOL
        reason = f"{symbol.kind} {symbol.name} defined at lines {symbol.start}-{symbol.end}"
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    trace_files = {p.split(":")[0] for p in traceback_paths}
    for path in trace_files:
        for key, entry in buckets.items():
            if key[0] == path:
                entry["score"] += W_TRACEBACK
                entry["components"]["traceback"] = entry["components"].get("traceback", 0.0) + W_TRACEBACK
                reason = "appears in latest test traceback"
                if reason not in entry["reasons"]:
                    entry["reasons"].append(reason)

    ranked: list[SearchHit] = []
    for (path, start, end), entry in buckets.items():
        try:
            excerpt, _ = _excerpt(root, path, start, end)
            digest = sha256_file(root / path)
        except OSError:
            continue
        ranked.append(
            SearchHit(path, start, end, excerpt, digest, entry["score"], entry["components"], entry["reasons"])
        )
    ranked.sort(key=lambda h: (-h.score, h.path, h.start, h.end))
    return ranked[:MAX_HITS]
