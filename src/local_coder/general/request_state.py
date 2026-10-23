"""Where a request stands: waiting for Keep or Discard, kept, or discarded.

One definition for the review snapshot, History, Keep, Discard and the note a question gets, so
they can never disagree. The flags live in the request's meta.json. A kept change is in the
project, so "kept" wins when an old bug left both flags behind.

A request can also be built on an older one that still waits: it ran on the project's sticky working
copy while that copy held the older result (a hosted-agent run starts from the folder instead). That link
is `built_on` in the newer request's meta.json. Keeping the newer request puts the older result into the
project too, so the older request is closed by that Keep (`kept` plus `kept_with`); and the older one can
not be kept or discarded on its own while the newer one still has changes to decide.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

WAITING = "waiting"
KEPT = "kept"
DISCARDED = "discarded"

# Keys in meta.json: the request this one's working copy started from, and the later request whose Keep
# carried this one into the project.
BUILT_ON = "built_on"
KEPT_WITH = "kept_with"

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


def built_on(metas: Mapping[str, Any], request_id: str) -> list[str]:
    """The requests this one was built on, nearest first (each record names the one before it)."""
    chain: list[str] = []
    seen = {request_id}
    meta = metas.get(request_id)
    while isinstance(meta, dict):
        parent = meta.get(BUILT_ON)
        if not isinstance(parent, str) or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        meta = metas.get(parent)
    return chain


def waiting_on_top(metas: Mapping[str, Any], request_id: str) -> list[str]:
    """The requests built on this one (directly or through others) that are still undecided."""
    return sorted(
        other for other, meta in metas.items()
        if other != request_id and request_state(meta) == WAITING and request_id in built_on(metas, other)
    )
