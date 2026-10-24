"""Where a request stands: waiting for Keep or Discard, kept, or discarded.

One definition for the review snapshot, History, Keep, Discard and the note a question gets, so
they can never disagree. The flags live in the request's meta.json. A kept change is in the
project, so "kept" wins when an old bug left both flags behind. "In the project" is per file: a kept
request's files were written by a Keep, whatever a later request did to the lines in them.

A request can also be built on an older one that still waits: it ran on the project's sticky working
copy while that copy held the older result, whichever engine it used (the hosted agent included). That
link is `built_on` in the newer request's meta.json; a continuation carries it too, to the request it
continues. The older result is the base of the newer one, so keeping the newer request carries it into the
project, minus whatever the newer request took back. Keep closes every waiting request it was built on, nearest
first (`kept` plus `kept_with`), however many of their files it wrote; the walk stops at the first request with no
files, which published its starting copy and so carries nothing below it. The files of a closed request that the
Keep did not write are its `dropped` list: when there are some, History says `replaced` and the words say that
nothing from them went into those files. A request whose own result lists no files while its baseline lists some (a
folder that was emptied or made unreadable, or a request that deleted every file) has none to name: nothing is listed
as taken back from it. A request that is still open in its record although a kept request was
built on it (left by an older build, or a record that could not be saved) is read as closed from the kept request's
record; nothing is rewritten. A closed request can be neither kept nor discarded, and an older one can not be kept
or discarded on its own while the newer one still has changes to decide.

A Discard puts the working copy back to where the request started: the result it was built on while that still
waits, else its own baseline (a request with no files of its own leaves the copy as it is), so the stack
survives it. A request built on an older one that ends with the copy exactly as that result has no result of its
own: no files, no Keep or Discard (a `keep` that arrives anyway is refused, code `nothing_to_keep`), it does not hold
the older one back, and it keeps its History word. The next request passes over such requests and discarded ones and is
built on the newest one left.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

WAITING = "waiting"
KEPT = "kept"
DISCARDED = "discarded"

# Keys in meta.json: the request this one's working copy started from, the later request whose Keep carried
# this one into the project, and the files of this request that Keep did not write (only when there are some).
BUILT_ON = "built_on"
KEPT_WITH = "kept_with"
DROPPED = "dropped"

# The History word of a closed request that a later Keep took files back from.
REPLACED = "replaced"
REPLACED_SENTENCE = "These changes were replaced by a later request you kept."
NAMES_SHOWN = 3

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


def dropped_files(meta: Any) -> list[str]:
    """The files of this request that the Keep of a later request did not write, sorted; [] when all went in."""
    names = meta.get(DROPPED) if isinstance(meta, dict) else None
    return sorted({name for name in names if isinstance(name, str)}) if isinstance(names, list) else []


def replaced_sentence(files: Iterable[str]) -> str:
    """What is true of a request a later Keep took files back from, naming those files (never any model text).

    Sorted, each file once, at most three named: `a`, `a or b`, `a, b or c`, then `a, b, c or 2 more files`
    (`or 1 more file`). Needs at least one file.
    """
    names = sorted(set(files))
    if not names:
        raise ValueError("a replaced request has at least one file nothing went into")
    if len(names) > NAMES_SHOWN:
        more = len(names) - NAMES_SHOWN
        shown = f"{', '.join(names[:NAMES_SHOWN])} or {more} more file{'' if more == 1 else 's'}"
    elif len(names) > 1:
        shown = f"{', '.join(names[:-1])} or {names[-1]}"
    else:
        shown = names[0]
    return f"{REPLACED_SENTENCE} Nothing from them went into {shown}."


def request_outcome(meta: Any) -> str:
    """The word History shows: kept, replaced, discarded, how an agent run ended, or "review"."""
    state = request_state(meta)
    if state == KEPT and dropped_files(meta):
        return REPLACED
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
