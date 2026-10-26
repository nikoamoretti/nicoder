"""Chats without a project are saved and listed, and a run keeps going while the app shows another chat.

Nico (2026-10-26): "Created a chat that disappeared" and "creating a new chat while another one is running is
super buggy". A chat started without a project lived only on screen; now each one is saved under chats/free/,
listed newest first, and carried into the project it leads to.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppError, chat_request, start_chat
from local_coder.general.chat_log import (
    append_free_turn,
    archive_dir,
    list_free_chats,
    load_chat,
    load_free_chat,
)


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    return home


def _fake_llm(monkeypatch, reply: str) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": reply}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("local_coder.app_service.ensure_server", lambda wait_s=90: {"ok": True})
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def test_a_chat_without_a_project_is_saved_and_listed(config_home):
    append_free_turn("chat-aaaa1111", "user", "What does   this Chinese email say?")
    append_free_turn("chat-aaaa1111", "assistant", "It is a cold sales email.")
    append_free_turn("chat-bbbb2222", "user", "x" * 80)

    rows = list_free_chats()
    assert [row["id"] for row in rows] == ["chat-bbbb2222", "chat-aaaa1111"]  # the newest activity first
    assert rows[1]["title"] == "What does this Chinese email say?"
    assert rows[0]["title"].endswith("…") and len(rows[0]["title"]) == 60
    saved = load_free_chat("chat-aaaa1111")
    assert [turn["role"] for turn in saved["turns"]] == ["user", "assistant"]


def test_an_empty_chat_is_not_listed_and_a_bad_id_is_refused(config_home):
    assert load_free_chat("chat-never-saved")["turns"] == []
    assert list_free_chats() == []
    with pytest.raises(ValueError):
        load_free_chat("../../etc/passwd")


def test_a_question_without_a_project_saves_its_chat_and_reads_it_back(config_home, monkeypatch):
    _fake_llm(monkeypatch, "It is a cold sales email.")
    first = chat_request(None, "What does this Chinese email say?", chat_id="chat-cccc3333")
    assert [turn["text"] for turn in first["turns"]] == ["What does this Chinese email say?",
                                                         "It is a cold sales email."]
    captured = _fake_llm(monkeypatch, "You asked about the email.")
    chat_request(None, "What did I just ask?", chat_id="chat-cccc3333")
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert {"role": "user", "content": "What does this Chinese email say?"} in payload["messages"]
    assert len(load_free_chat("chat-cccc3333")["turns"]) == 4


def test_a_bad_chat_id_is_one_plain_sentence(config_home, monkeypatch):
    _fake_llm(monkeypatch, "Okay.")
    with pytest.raises(AppError, match="That chat does not exist"):
        chat_request(None, "hi", chat_id="../x")


def test_do_it_after_a_discussion_builds_what_was_discussed(config_home, tmp_path):
    append_free_turn("chat-dddd4444", "user", "Could you build a task board for my team?")
    append_free_turn("chat-dddd4444", "assistant", "Yes: columns for to do, doing and done.")

    result = start_chat("do it", parent=tmp_path / "Projects", chat_id="chat-dddd4444")

    dest = Path(result["path"])
    assert dest.name == "task-board-my-team"  # named after the discussion, not "do-it"
    assert result["moved_chat"] is True
    assert [turn["text"] for turn in load_chat(dest)["turns"]] == [
        "Could you build a task board for my team?", "Yes: columns for to do, doing and done.",
    ]
    assert list_free_chats() == []  # the chat continues in the project
    kept = list(archive_dir().glob("free-chat-dddd4444-*.json"))
    assert len(kept) == 1 and len(json.loads(kept[0].read_text())["turns"]) == 2  # moved, never deleted


def test_a_new_chat_that_starts_with_work_moves_nothing(config_home, tmp_path):
    result = start_chat("Build a task board", parent=tmp_path, chat_id="chat-eeee5555")
    assert result["moved_chat"] is False
    assert Path(result["path"]).name == "task-board"
    assert load_chat(result["path"])["turns"] == []


def test_the_app_lists_and_loads_saved_chats(config_home):
    append_free_turn("chat-ffff6666", "user", "hello")
    server = RpcServer(out=io.StringIO())
    listed = server.dispatch({"id": "1", "method": "list_chats", "params": {}})["result"]["chats"]
    assert [row["id"] for row in listed] == ["chat-ffff6666"]
    loaded = server.dispatch({"id": "2", "method": "load_chat", "params": {"chat_id": "chat-ffff6666"}})
    assert [turn["text"] for turn in loaded["result"]["turns"]] == ["hello"]


def test_stopping_a_question_leaves_a_running_change_alone(config_home):
    class Runtime:
        cancelled = 0

        def cancel(self):
            Runtime.cancelled += 1
            return {"ok": True}

        def subscribe(self, listener):
            pass

    server = RpcServer(runtime=Runtime(), out=io.StringIO())
    server.dispatch({"id": "1", "method": "cancel", "params": {"what": "chat"}})
    assert server.chat_cancel.is_set() and Runtime.cancelled == 0
    server.chat_cancel.clear()
    server.dispatch({"id": "2", "method": "cancel", "params": {"what": "run"}})
    assert not server.chat_cancel.is_set() and Runtime.cancelled == 1
    server.dispatch({"id": "3", "method": "cancel", "params": {}})
    assert server.chat_cancel.is_set() and Runtime.cancelled == 2


def test_a_questions_steps_are_marked_as_the_chats(config_home, monkeypatch):
    out = io.StringIO()
    server = RpcServer(out=out)

    def fake_chat(path, text, *, listener=None, **_kwargs):
        listener({"type": "activity", "phrase": "Reading notes.md"})
        return {"kind": "chat", "summary": "ok"}

    monkeypatch.setattr("local_coder.app_rpc.chat_request", fake_chat)
    server.dispatch({"id": "1", "method": "chat_request", "params": {"text": "hi", "chat_id": "chat-gggg7777"}})
    events = [json.loads(line) for line in out.getvalue().splitlines() if '"event"' in line]
    assert events and all(event["payload"]["source"] == "chat" for event in events)


def test_show_me_the_app_waits_while_a_change_uses_the_preview(config_home, tmp_path, monkeypatch):
    from local_coder.general import coordinator

    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
    started: list[object] = []
    monkeypatch.setattr("local_coder.app_service._start_preview_note",
                        lambda repo: started.append(repo) or ({"ok": True}, "running"))
    captured = _fake_llm(monkeypatch, "Not yet.")
    with coordinator._request_running("other-project", "req-1"):
        result = chat_request(root, "show me the app")
    assert started == [] and "preview" not in result
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert "uses the preview" in json.dumps(payload["messages"])
    # The project's own run, read as a question ("run the app"), may start the preview.
    with coordinator._request_running(coordinator.project_id_for(root), "req-2"):
        chat_request(root, "show me the app")
    assert started == [root.resolve()]


def _collect_runtime(tmp_path):
    from local_coder.app_service import AppRuntime

    runtime = AppRuntime(state_dir=tmp_path / "state")
    events: list[dict] = []
    runtime.subscribe(events.append)
    return runtime, events


def _wait_idle(runtime):
    import time

    deadline = time.monotonic() + 10
    while runtime.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_change_the_project_reads_as_a_question_still_ends(config_home, tmp_path, monkeypatch):
    # "run the app" from a chat: the project reads it as a question, answers it in its chat and sends no end event.
    monkeypatch.setattr("local_coder.general.coordinator.run_general_request",
                        lambda *args, **kwargs: {"kind": "chat", "summary": "It runs on port 5173."})
    runtime, events = _collect_runtime(tmp_path)
    started = runtime.start_request(str(tmp_path), "run the app")
    _wait_idle(runtime)
    ends = [event for event in events if event["type"] == "done"]
    assert len(ends) == 1
    assert ends[0]["data"] == {"request_id": started["episode_id"], "answered": True}


def test_a_run_that_ends_itself_gets_no_second_end(config_home, tmp_path, monkeypatch):
    def run(*args, listener=None, request_id=None, **kwargs):
        listener({"type": "budget", "data": {"detail": "Out of steps."}})
        listener({"type": "done", "data": {"request_id": request_id}})
        return {"kind": "review"}

    monkeypatch.setattr("local_coder.general.coordinator.run_general_request", run)
    runtime, events = _collect_runtime(tmp_path)
    runtime.start_request(str(tmp_path), "add a footer")
    _wait_idle(runtime)
    assert [event["type"] for event in events] == ["budget", "done"]


def test_a_failed_run_ends_with_its_error_only(config_home, tmp_path, monkeypatch):
    def run(*args, **kwargs):
        raise AppError("The model is not reachable.")

    monkeypatch.setattr("local_coder.general.coordinator.run_general_request", run)
    runtime, events = _collect_runtime(tmp_path)
    runtime.start_request(str(tmp_path), "add a footer")
    _wait_idle(runtime)
    assert [event["type"] for event in events] == ["app_error"]


def test_the_next_change_waits_for_the_last_one_to_close(config_home, tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setattr("local_coder.general.coordinator.run_general_request",
                        lambda *args, **kwargs: {"kind": "review"})
    runtime, _events = _collect_runtime(tmp_path)
    closing = threading.Thread(target=time.sleep, args=(0.3,), daemon=True)
    closing.start()
    runtime._thread = closing  # a run that sent its end event and is still closing
    assert runtime.start_request(str(tmp_path), "add a footer")["started"] is True
    _wait_idle(runtime)


def test_a_question_that_fails_stays_in_its_chat(config_home, monkeypatch):
    def broken(*args, **kwargs):
        raise AppError("The model is not reachable.")

    monkeypatch.setattr("local_coder.app_service._local_chat_completion", broken)
    monkeypatch.setattr("local_coder.app_service.ensure_server", lambda wait_s=90: {"ok": True})
    with pytest.raises(AppError):
        chat_request(None, "What does this email say?", chat_id="chat-hhhh8888")
    assert [turn["text"] for turn in load_free_chat("chat-hhhh8888")["turns"]] == ["What does this email say?"]
    assert [row["id"] for row in list_free_chats()] == ["chat-hhhh8888"]


def test_a_chat_id_with_a_line_break_is_refused(config_home):
    with pytest.raises(ValueError):
        load_free_chat("chat-aaaa1111\n")
