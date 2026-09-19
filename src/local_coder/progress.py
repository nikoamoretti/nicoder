"""Map controller-owned events to ordinary-language progress.

Phrases are derived from recorded event types. Nothing here invents
percentages, fake activity, or a solved claim the controller did not make.
"""
from __future__ import annotations

from typing import Any

# First matching event type wins when scanning newest-first.
_EVENT_PHRASES: tuple[tuple[str, str], ...] = (
    ("accepted", "Changes were kept in the project."),
    ("discarded", "Proposed changes were discarded. The original project is unchanged."),
    ("accept_failed", "Keeping the changes was refused. The original project was left as it was."),
    ("handoff_written", "Finished this request."),
    ("done_validated", "Checking whether the request is complete."),
    ("auto_check_completed", "Checking the change."),
    ("check_completed", "Checking the change."),
    ("check_intent", "Checking the change."),
    ("patch_applied", "Preparing a change."),
    ("patch_reverted", "Reverting a change that did not hold."),
    ("patch_rejected", "A proposed edit was refused."),
    ("patch_intent", "Preparing a change."),
    ("model_response", "Reading the relevant code."),
    ("model_request", "Reading the relevant code."),
    ("adapter_error", "The local model did not complete that step."),
    ("episode_created", "Inspecting the project."),
    ("resumed", "Continuing the previous request."),
    ("cancelled", "Stopped at your request."),
    ("preparing", "Making a working copy of the project."),
    ("model_starting", "Loading the local model. This can take a minute."),
    ("engine_starting", "Starting the coding engine."),
    ("prompt_sent", "The local model is writing code. This can take several minutes."),
    ("preview", "Opening a preview."),
    ("done", "Finished this attempt. Review the result."),
    ("stopped", "Stopped."),
    ("budget", "Reached the time or step limit for this request."),
)


def phrase_for_event(event_type: str, data: dict[str, Any] | None = None) -> str | None:
    """One user-facing sentence for a known event type, or None."""
    data = data or {}
    if event_type == "engine_starting":
        detail = str(data.get("detail") or "").strip()
        if detail:
            return detail
        if data.get("reused"):
            return "Reusing the coding engine."
        return "Starting the coding engine."
    if event_type == "check_intent" and data.get("scope") == "baseline":
        return "Running the project's existing tests."
    if event_type == "check_completed" and data.get("scope") == "baseline":
        return "Finished the starting tests."
    if event_type == "check_intent" and data.get("scope") == "final":
        return "Running the final check."
    if event_type == "check_completed" and data.get("scope") == "final":
        status = data.get("status")
        if status == "pass":
            return "The final check passed."
        if status == "fail":
            return "The final check found a problem."
        return "The final check did not finish cleanly."
    if event_type in {"activity", "tool"}:
        detail = str(data.get("detail") or "").strip()
        if detail:
            return detail
        tool = str(data.get("tool") or "a tool")
        return f"Using {tool}."
    if event_type == "working":
        last = str(data.get("last") or "Waiting for the next update.")
        return f"Still working — {data.get('elapsed_s', 0)} seconds so far. {last}"
    if event_type == "file_changed":
        return f"Changed {data.get('path') or 'a file'}."
    if event_type in {"error", "app_error"}:
        return str(data.get("detail") or "Something went wrong.")
    if event_type == "handoff_written":
        outcome = data.get("outcome")
        if outcome == "solved":
            return "Ready to review."
        if outcome == "blocked":
            return "Stopped — here is what prevented completion."
        if outcome == "setup_failed":
            return "Stopped — the project was not ready to run this request."
        if outcome == "runtime_failed":
            return "Stopped — the local machinery hit an error."
    for name, phrase in _EVENT_PHRASES:
        if name == event_type:
            return phrase
    return None


def latest_progress(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Newest understandable progress phrase from an event list."""
    for event in reversed(events):
        phrase = phrase_for_event(str(event.get("type", "")), event.get("data") or {})
        if phrase:
            return {
                "phrase": phrase,
                "event_type": event.get("type"),
                "seq": event.get("seq"),
            }
    return {"phrase": "Starting.", "event_type": None, "seq": None}
