"""Progress phrases for the general engine must reach the Mac UI."""

from local_coder.app_service import AppRuntime
from local_coder.general.coordinator import _phrase
from local_coder.progress import phrase_for_event


def test_phrase_for_event_covers_general_engine_steps():
    assert phrase_for_event("preparing")
    assert phrase_for_event("model_starting")
    assert phrase_for_event("engine_starting")
    assert "minutes" in (phrase_for_event("prompt_sent") or "").lower()
    assert "read" in (phrase_for_event("tool", {"tool": "read"}) or "").lower()
    assert "App.tsx" in (phrase_for_event("file_changed", {"path": "src/App.tsx"}) or "")


def test_runtime_keeps_coordinator_phrase_when_unmapped():
    runtime = AppRuntime()
    seen: list[dict] = []
    runtime.subscribe(seen.append)
    runtime._emit({
        "type": "custom_step",
        "data": {},
        "phrase": "Copying the project…",
        "seq": None,
    })
    assert seen[0]["phrase"] == "Copying the project…"


def test_coordinator_phrases_say_where_the_run_is():
    assert "working copy" in _phrase("preparing", {}).lower()
    assert "model" in _phrase("model_starting", {}).lower()
    assert "engine" in _phrase("engine_starting", {}).lower()
    assert "minutes" in _phrase("prompt_sent", {}).lower()
    assert "proxy" in _phrase("activity", {"detail": "Starting the local model proxy."}).lower()
