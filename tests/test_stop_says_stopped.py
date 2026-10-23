"""A Stop says "Stopped" (cut core-fixes, problem 5).

A cancelled run ends with one `stopped` event ("Stopped."), never `done` ("Finished this attempt..."), on the
agent path, the local-model path and the fake-engine path. If nothing was written, the summary says so, and
Keep is offered after a Stop only when files exist.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from local_coder.general.coordinator import load_meta, run_general_request

TERMINAL = {"done", "stopped"}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    return root


@pytest.fixture
def project(home) -> Path:
    root = home / "projects" / "tool"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "tool"\n', encoding="utf-8")
    (root / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _terminal(events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] in TERMINAL]


def _assert_ends_with_stopped(events: list[dict], review: dict) -> None:
    ended = _terminal(events)
    assert [event["type"] for event in ended] == ["stopped"], "exactly one terminal event, and it is a Stop"
    assert ended[0]["phrase"] == "Stopped."
    assert events[-1] is ended[0], "nothing follows the Stop"
    assert review["summary"].startswith("Stopped")
    assert "Finished" not in str(review["model_commentary"])
    assert review["verification"]["stopped_incomplete"] is True


def test_a_stop_before_anything_was_written_says_nothing_was_changed(project):
    cancel = threading.Event()
    before = _tree(project)

    def model(_messages, _tools):
        cancel.set()
        return _reply(calls=[_call("write", {"path": "new.py", "content": "x = 1\n"}, "1")])

    events: list[dict] = []
    review = run_general_request(
        project, "Add new.py", agent_chat=model, cancel_event=cancel, listener=events.append,
        start_preview_after=False,
    )
    _assert_ends_with_stopped(events, review)
    assert review["summary"] == "Stopped. Nothing was changed."
    assert review["model_commentary"] == "Stopped. Nothing was changed."
    assert review["can_keep"] is False
    assert review["facts"]["files"] == []
    assert _tree(project) == before, "the project folder is untouched"


def test_a_stop_after_some_files_were_written_still_offers_them(project):
    cancel = threading.Event()
    calls = {"n": 0}

    def model(_messages, _tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return _reply(calls=[_call("write", {"path": "new.py", "content": "x = 1\n"}, "1")])
        cancel.set()
        return _reply(calls=[_call("write", {"path": "later.py", "content": "y = 2\n"}, "2")])

    events: list[dict] = []
    review = run_general_request(
        project, "Add files", agent_chat=model, cancel_event=cancel, listener=events.append,
        start_preview_after=False,
    )
    _assert_ends_with_stopped(events, review)
    assert review["facts"]["files"] == ["new.py"]
    assert review["can_keep"] is True
    assert review["summary"] == "Stopped. Review what changed so far, or discard it."


def test_a_stop_on_the_local_model_path_ends_with_stopped(project):
    cancel = threading.Event()

    def complete(_prompt: str) -> str:
        cancel.set()
        return "```tool.py\nprint('late')\n```\n"

    events: list[dict] = []
    review = run_general_request(
        project, "Change the greeting", fake_complete=complete, cancel_event=cancel, listener=events.append,
        start_preview_after=False, model_id="Qwen3.5-4B-Q4_K_M",
    )
    _assert_ends_with_stopped(events, review)
    assert review["can_keep"] is False
    assert "planning" not in review["summary"], "a Stop is not 'the model stopped after planning'"
    assert (project / "tool.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_a_stop_in_a_fake_engine_run_ends_with_stopped(project):
    cancel = threading.Event()

    def engine(candidate: Path, _text: str, stop: threading.Event | None) -> None:
        (candidate / "tool.py").write_text("print('half')\n", encoding="utf-8")
        assert stop is not None
        stop.set()

    events: list[dict] = []
    review = run_general_request(
        project, "Change it", fake_engine=engine, cancel_event=cancel, listener=events.append,
        start_preview_after=False,
    )
    _assert_ends_with_stopped(events, review)
    assert review["can_keep"] is True and review["facts"]["files"] == ["tool.py"]


def test_a_run_that_was_not_stopped_still_ends_with_done(project):
    def engine(candidate: Path, _text: str, _stop) -> None:
        (candidate / "tool.py").write_text("print('hello')\n", encoding="utf-8")

    events: list[dict] = []
    review = run_general_request(
        project, "Change it", fake_engine=engine, listener=events.append, start_preview_after=False,
    )
    ended = _terminal(events)
    assert [event["type"] for event in ended] == ["done"]
    assert ended[0]["phrase"] == "Finished this attempt. Review the result."
    assert not review["summary"].startswith("Stopped")
    assert load_meta(review["project_id"], review["request_id"]).get("stop_reason") != "cancelled"
