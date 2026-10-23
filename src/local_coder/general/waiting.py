"""The result that is waiting for Keep or Discard, described for a question about it.

A question asked while a change waits would otherwise be answered from the project folder, which does not
hold the change yet, and the model would call correct work "never applied". Local Coder tells the model
what is waiting, from the request's own record and never from model text. The note is for the model only:
it is not saved in the chat.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from local_coder.general.coordinator import (
    _request_in_flight,
    _title_from_transcript,
    project_dir,
    project_id_for,
    request_delta,
)
from local_coder.general.request_state import WAITING, request_state
from local_coder.general.snapshot import Delta

# Only the newest open requests are looked at; most have no files (a question, a stop before any write, a follow-up
# that failed or changed nothing and so has no result of its own), and a run of those must not hide the result that
# still waits behind them.
LOOK_AT = 30
LISTED_FILES = 8


@dataclass(frozen=True)
class WaitingResult:
    """A request whose files wait for Keep or Discard."""

    request_id: str
    title: str
    created_ts: float
    delta: Delta
    candidate: Path

    @property
    def files(self) -> list[str]:
        return sorted(self.delta.touched())


def find_waiting_result(user_path: str | Path) -> WaitingResult | None:
    """The project's newest request that is neither kept nor discarded and changed files, or None.

    Open requests that changed nothing (a question, an early Stop) are passed over: there is nothing
    in them to wait for. Reads only; nothing is created.
    """
    project_id = project_id_for(Path(user_path).expanduser())
    if _request_in_flight(project_id):
        return None  # a result that is still being made is not a waiting one
    folder = project_dir(project_id) / "requests"
    if not folder.is_dir():
        return None
    open_requests: list[tuple[float, Path, dict[str, object]]] = []
    for entry in folder.iterdir():
        try:
            meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and request_state(meta) == WAITING:
            open_requests.append((float(meta.get("created_ts") or 0), entry, meta))
    open_requests.sort(key=lambda row: row[0], reverse=True)
    for created, entry, meta in open_requests[:LOOK_AT]:
        delta = request_delta(meta)
        if delta is not None and delta.touched():
            title = str(meta.get("title") or "").strip() or _title_from_transcript(entry)
            return WaitingResult(
                request_id=str(meta.get("request_id") or entry.name),
                title=title,
                created_ts=created,
                delta=delta,
                candidate=Path(str(meta["candidate"])),
            )
    return None


def age_text(seconds: float) -> str:
    """"just now", "5 minutes ago", "2 hours ago", "3 days ago"."""
    seconds = max(0.0, seconds)
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def _what_changed(delta: Delta) -> str:
    parts: list[str] = []
    listed = 0
    total = len(delta.touched())

    def take(label: str, names: list[str]) -> None:
        nonlocal listed
        room = LISTED_FILES - listed
        if names and room > 0:
            shown = names[:room]
            listed += len(shown)
            parts.append(f"{label} {', '.join(shown)}")

    take("changes", delta.modified)
    take("adds", delta.added)
    take("removes", delta.deleted)
    take("renames", [f"{old} to {new}" for old, new in delta.renamed])
    text = "; ".join(parts)
    if total > listed:
        text += f" (and {total - listed} more)"
    return text


def waiting_note(user_path: str | Path | None, *, can_read: bool) -> str:
    """The sentence a question gets when a result waits for Keep or Discard, or "" when none does.

    `can_read` is True for the agent (it has read tools and gets the waiting copy's path) and False for
    a local model without tools (it only learns which files and that they are not in the folder yet).
    Any trouble reading the record gives "": a bad meta.json must never fail a question.
    """
    if user_path is None:
        return ""
    try:
        waiting = find_waiting_result(user_path)
        if waiting is None:
            return ""
        what = _what_changed(waiting.delta)
        if not can_read:
            return f"A change is waiting for the user to Keep or Discard and is not in the folder yet. It {what}."
        title = f" “{waiting.title}”" if waiting.title else ""
        age = age_text(time.time() - waiting.created_ts)
        return (
            f"Local Coder's own record, not the user's words: the request{title} (made {age}) {what}. "
            "That result is waiting for the user to Keep or Discard, so it is not in the folder yet: the "
            "project's own files still show the old code, which does not mean the change failed. "
            f"To check what the change does, read the waiting copy (read-only) in {waiting.candidate}, "
            "using the same relative paths. Do not open it as a project."
        )
    except Exception:  # noqa: BLE001 — a bad record must never fail a question
        return ""
