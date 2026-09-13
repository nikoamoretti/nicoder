"""FIND: deterministic retrieval as code, not model-invented shell."""
from __future__ import annotations

from pathlib import Path

from ..retrieval.inventory import Inventory
from ..retrieval.lexical import literal_search, tokenize
from ..retrieval.python_symbols import index_symbols, match_symbols
from ..retrieval.ranking import SearchHit, rank
from ..state import FocusRange


def run_find(
    candidate_root: Path,
    inventory: Inventory,
    query: str,
    symbols: list[str],
    traceback_paths: list[str],
    ticket: str,
    max_focus_files: int,
    existing_focus: list[FocusRange],
) -> tuple[list[SearchHit], list[FocusRange], dict]:
    """Search permitted paths and propose focus ranges.

    Returns (hits, new_focus_entries, observation). Missing results are an
    honest empty result, never guessed file contents.
    """
    tokens = tokenize(f"{query} {ticket}")
    lex_hits = literal_search(candidate_root, inventory.files, tokens)
    index = index_symbols(candidate_root, inventory.python_files())
    sym_hits = match_symbols(index, symbols)
    hits = rank(candidate_root, lex_hits, sym_hits, traceback_paths)
    known = {f.path for f in existing_focus}
    fresh: list[FocusRange] = []
    for hit in hits:
        if hit.path in known or any(f.path == hit.path for f in fresh):
            continue
        if len(known) + len(fresh) >= max_focus_files:
            break
        fresh.append(
            FocusRange(
                path=hit.path,
                start=hit.start,
                end=hit.end,
                sha256=hit.file_sha,
                reason="; ".join(hit.reasons[:3]) or "retrieved evidence",
            )
        )
    observation: dict = {
        "status": "ok" if hits else "empty",
        "hits": [
            {
                "path": h.path,
                "start": h.start,
                "end": h.end,
                "file_sha": h.file_sha[:16],
                "score": round(h.score, 2),
                "components": h.components,
                "reasons": h.reasons[:3],
            }
            for h in hits
        ],
        "new_focus": [f.path for f in fresh],
        "focus_limit": max_focus_files,
    }
    if not hits:
        observation["note"] = "no permitted evidence matched; refine query or stop blocked"
    return hits, fresh, observation
