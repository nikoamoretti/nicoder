"""Chat replies for ordinary questions that are not coding jobs."""
from __future__ import annotations

import httpx
import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppError, chat_request, classify_request
from local_coder.general.activity import assistant_text_from_messages
from local_coder.general.chat_log import append_turn, recent_messages
from local_coder.general.coordinator import run_general_request


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    return home


def _install_fake_llm(monkeypatch, reply: str = "Okay."):
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
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("local_coder.app_service.ensure_server", lambda wait_s=90: {"ok": True})
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def test_classify_talk_look_act_rules():
    assert classify_request("open the app")["kind"] == "chat"
    assert classify_request("are you able to open the app for me?")["kind"] == "chat"
    assert classify_request("start the preview")["kind"] == "chat"
    assert classify_request("can you tell me how to run the app?")["kind"] == "chat"
    assert classify_request("Can you make this a standalone mac app?")["kind"] == "code"
    assert classify_request("can you make the app the mac app that is easily openable")["kind"] == "code"
    assert classify_request("wrap this as a Mac app")["kind"] == "code"
    assert classify_request("is this a Mac app?")["kind"] == "chat"
    assert classify_request("the page is blank")["kind"] == "code"


def test_classify_location_question_is_chat():
    assert classify_request("where is the app located?")["kind"] == "chat"
    assert classify_request("Where are the project files?")["kind"] == "chat"


def test_classify_question_without_edit_verbs_is_chat():
    assert classify_request("what should I try next?")["kind"] == "chat"
    assert classify_request("is this a React project?")["kind"] == "chat"


def test_classify_keeps_edits_and_explain():
    assert classify_request("Add due dates.")["kind"] == "code"
    assert classify_request("Can you add a search box?")["kind"] == "code"
    assert classify_request("Explain how add works")["kind"] == "explain"


def test_chat_questions_use_the_local_model(tmp_path, config_home, monkeypatch):
    root = tmp_path / "task-board-4"
    root.mkdir()
    (root / "package.json").write_text('{"name":"tb"}\n', encoding="utf-8")
    captured = _install_fake_llm(monkeypatch, "It lives in that project folder on your Mac.")
    result = chat_request(root, "where is the app located?")
    assert result["kind"] == "chat"
    assert result["summary"] == "It lives in that project folder on your Mac."
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["messages"][-1]["content"] == "where is the app located?"
    assert result["hits"] == []
    assert result["turns"][0]["role"] == "user"
    assert result["turns"][1]["role"] == "assistant"


def test_chat_mac_app_question_uses_the_model(tmp_path, config_home, monkeypatch):
    root = tmp_path / "task-board-4"
    root.mkdir()
    (root / "package.json").write_text(
        '{"name":"tb","scripts":{"dev":"vite"},"dependencies":{"react":"19"}}\n',
        encoding="utf-8",
    )
    captured = _install_fake_llm(monkeypatch, "This folder is a website today, not a Mac .app.")
    result = chat_request(root, "Is this a Mac app?")
    assert result["kind"] == "chat"
    assert result["summary"] == "This folder is a website today, not a Mac .app."
    assert "preview" not in result
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["messages"][-1]["content"] == "Is this a Mac app?"
    system = payload["messages"][0]["content"]
    assert "Do not paste the fact list" in system
    assert "outside Local Coder" in system


def test_chat_open_app_starts_preview_and_asks_the_model(tmp_path, config_home, monkeypatch):
    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text('{"name":"web","scripts":{"dev":"vite"}}\n', encoding="utf-8")

    def fake_preview(project_dir, project_id, wait_s=45):
        return {
            "ok": True,
            "started": True,
            "url": "http://127.0.0.1:4173/",
            "project_id": project_id,
            "dir": str(project_dir),
        }

    monkeypatch.setattr("local_coder.general.preview.start_preview", fake_preview)
    captured = _install_fake_llm(monkeypatch, "Preview is up.")
    result = chat_request(root, "open the app")
    assert result["kind"] == "chat"
    assert result["summary"] == "Preview is up."
    assert result["preview"]["url"] == "http://127.0.0.1:4173/"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert "http://127.0.0.1:4173/" in payload["messages"][-1]["content"]


def test_chat_blank_page_uses_the_model(tmp_path, config_home, monkeypatch):
    root = tmp_path / "task-board-3"
    root.mkdir()
    (root / "package.json").write_text('{"name":"tb","scripts":{"dev":"vite"}}\n', encoding="utf-8")
    (root / "index.html").write_text('<script type="module" src="/src/main.tsx"></script>\n', encoding="utf-8")
    captured = _install_fake_llm(monkeypatch, "The starter is still empty, so the page looks blank.")
    result = chat_request(root, "the page is blank")
    assert result["kind"] == "chat"
    assert result["summary"] == "The starter is still empty, so the page looks blank."
    payload = captured["json"]
    assert isinstance(payload, dict)
    system = payload["messages"][0]["content"]
    assert "src/main.tsx" in system


def test_chat_uses_local_model(tmp_path, config_home, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text(
        '{"name":"demo","scripts":{"dev":"vite"},"dependencies":{"react":"19"}}\n',
        encoding="utf-8",
    )
    captured = _install_fake_llm(monkeypatch, "Yes — this is a React/Vite web project.")
    result = chat_request(root, "Does it work well?")
    assert result["kind"] == "chat"
    assert "React" in result["summary"]
    payload = captured["json"]
    assert isinstance(payload, dict)
    system = payload["messages"][0]["content"]
    assert "already have the project facts" in system
    assert "outside Local Coder" in system
    assert "Project folder:" in system
    assert payload["messages"][-1]["content"] == "Does it work well?"
    assert result["model_commentary"] == result["summary"]


def test_recent_messages_clips_long_turns(tmp_path, config_home):
    root = tmp_path / "app"
    root.mkdir()
    append_turn(root, "user", "hello")
    append_turn(root, "assistant", "x" * 800)
    rows = recent_messages(root, limit=8, max_chars=20)
    assert rows[0] == {"role": "user", "content": "hello"}
    assert rows[1]["role"] == "assistant"
    assert rows[1]["content"].endswith("…")
    assert len(rows[1]["content"]) == 20


def test_chat_sends_recent_turns(tmp_path, config_home, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
    _install_fake_llm(monkeypatch, "It is a task board.")
    chat_request(root, "What is this app?")
    captured = _install_fake_llm(monkeypatch, "You asked what the app is.")
    result = chat_request(root, "What did I just ask?")
    assert result["kind"] == "chat"
    payload = captured["json"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert messages[0]["role"] == "system"
    assert {"role": "user", "content": "What is this app?"} in messages
    assert {"role": "assistant", "content": "It is a task board."} in messages
    assert messages[-1]["content"] == "What did I just ask?"


def test_act_is_remembered_in_later_chat(tmp_path, config_home, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "App.tsx").write_text(
        "export default function App(){return <p>hi</p>}\n", encoding="utf-8",
    )

    def complete(prompt: str) -> str:
        return "```src/App.tsx\nexport default function App(){return <h1>Board</h1>}\n```\n"

    run_general_request(
        root,
        "Build a task board",
        request_id="req-remember",
        fake_complete=complete,
        start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    captured = _install_fake_llm(monkeypatch, "You asked me to build a task board.")
    chat_request(root, "What did I ask you to build?")
    payload = captured["json"]
    assert isinstance(payload, dict)
    contents = [str(row.get("content") or "") for row in payload["messages"]]
    assert any("Build a task board" in item for item in contents)


def test_talk_through_coordinator_does_not_write_files(tmp_path, config_home, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "App.tsx").write_text("export const App = () => null\n", encoding="utf-8")
    original = (root / "src" / "App.tsx").read_text(encoding="utf-8")
    _install_fake_llm(monkeypatch, "Yes, this is a React project.")
    result = run_general_request(
        root,
        "Is this a React project?",
        request_id="req-talk",
        start_preview_after=False,
    )
    assert result["kind"] == "chat"
    assert result["summary"] == "Yes, this is a React project."
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == original


def test_chat_refuses_missing_folder(tmp_path, config_home):
    with pytest.raises(AppError, match="does not exist"):
        chat_request(tmp_path / "missing", "where is the app?")


def test_rpc_chat_request(tmp_path, config_home, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    _install_fake_llm(monkeypatch, "The project folder is on this Mac.")
    server = RpcServer()
    reply = server.dispatch({
        "id": "1",
        "method": "chat_request",
        "params": {"path": str(root), "text": "where is the project folder?"},
    })
    assert reply["ok"] is True
    assert reply["result"]["kind"] == "chat"
    assert reply["result"]["summary"] == "The project folder is on this Mac."
    loaded = server.dispatch({
        "id": "2",
        "method": "load_chat",
        "params": {"path": str(root)},
    })
    assert loaded["ok"] is True
    assert len(loaded["result"]["turns"]) == 2


def test_assistant_text_from_messages_collects_text_parts():
    messages = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
        {
            "info": {"role": "assistant"},
            "parts": [
                {"type": "tool", "tool": "bash", "state": {"input": {"command": "ls"}}},
                {"type": "text", "text": "The app lives in this folder."},
            ],
        },
    ]
    assert assistant_text_from_messages(messages) == "The app lives in this folder."
