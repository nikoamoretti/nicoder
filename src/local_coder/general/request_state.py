"""Where a request stands: waiting for Keep or Discard, kept, or discarded.

One definition for the review snapshot, History, Keep, Discard and the note a question gets, so
they can never disagree. The flags live in the request's meta.json. A kept change is in the
project, so "kept" wins when an old bug left both flags behind.
"""
from __future__ import annotations

from typing import Any

WAITING = "waiting"
KEPT = "kept"
DISCARDED = "discarded"

# How an agent run can end before its result is reviewed; History shows these words as they are.
_AGENT_ENDS = frozenset({"stopped", "time_limit", "step_limit", "error"})


def request_state(meta: Any) -> str:
    """"kept", "discarded", or "waiting" (neither: whatever the request produced is still undecided)."""
    if not isinstance(meta, dict):
        return WAITING
    if meta.get("kept"):
        return KEPT
    if meta.get("discarded"):
        return DISCARDED
    return WAITING


def is_closed(meta: Any) -> bool:
    """True once the request was kept or discarded: its result can no longer be decided."""
    return request_state(meta) != WAITING


def request_outcome(meta: Any) -> str:
    """The word History shows: kept, discarded, how an agent run ended, or "review"."""
    state = request_state(meta)
    if state != WAITING:
        return state
    agent = meta.get("agent") if isinstance(meta, dict) else None
    status = agent.get("status") if isinstance(agent, dict) else None
    return str(status) if status in _AGENT_ENDS else "review"
