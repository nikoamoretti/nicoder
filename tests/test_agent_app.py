"""The agent inside the app: questions without a project, Stop mid-question, change requests."""
from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import chat_request, classify_request, load_prefs
from local_coder.general.chat_log import load_chat
from local_coder.general.coordinator import load_meta, run_general_request

STARTER_BLOCK = (
    "<h1>New app</h1>\n      <p>This starter is empty on purpose. Describe what you want to build.</p>"
)
HOSTED = machine.MachineConfig(
    model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b",
    endpoint="https://integrate.api.nvidia.com",
    provider="nvidia",
    api_key_name="NVIDIA_API_KEY",
    n_ctx=131072,
    max_tokens=16384,
    quantization="hosted",
)


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    def __init__(self, *replies: dict) -> None:
        self.replies = list(replies)
        self.sent: list[list[dict]] = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        self.sent.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    repo = root / "projects" / "prototypes"
    (repo / "app" / "prototypes" / "shadow-paris").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "nicos-prototypes"}', encoding="utf-8")
    (repo / "README.md").write_text("# Nico — Prototypes\n", encoding="utf-8")
    (repo / "app" / "prototypes" / "shadow-paris" / "page.tsx").write_text("<h1>Shadow Paris</h1>\n")
    monkeypatch.setenv("HOME", str(root))
    return root


def _use_hosted(monkeypatch, script: Script | None = None) -> None:
    from local_coder.agent import runner

    machine.write_machine_config(HOSTED)
    if script is not None:
        def fake(cfg, body, *, thinking):
            return script(body["messages"], body.get("tools") or [])

        monkeypatch.setattr(runner, "hosted_chat", fake)


def test_explain_requests_become_questions_with_the_hosted_model(monkeypatch, home):
    assert classify_request("Explain how add works")["kind"] == "explain"
    _use_hosted(monkeypatch)
    assert classify_request("Explain how add works")["kind"] == "chat"
    assert classify_request("Add due dates.")["kind"] == "code"


def test_question_without_a_project_finds_and_opens_the_repo(monkeypatch, home):
    repo = home / "projects" / "prototypes"
    script = Script(
        _reply(calls=[_call("find_project", {"query": "paris shadow"}, "1")]),
        _reply(calls=[_call("open_project", {"path": str(repo)}, "2"),
                      _call("read", {"path": str(repo / "app/prototypes/shadow-paris/page.tsx")}, "3")]),
        _reply("Shadow Paris lives in app/prototypes/shadow-paris of your prototypes repo."),
    )
    _use_hosted(monkeypatch, script)
    events: list[dict] = []
    result = chat_request(None, "Check the repo containing the Paris Shadow app", listener=events.append)
    assert result["open_project"] == str(repo)
    assert result["status"] == "answered"
    assert result["files_read"] == ["~/projects/prototypes/app/prototypes/shadow-paris/page.tsx"]
    assert "1. ~/projects/prototypes" in script.sent[1][-1]["content"]
    turns = load_chat(repo)["turns"]
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert result["turns"][-1]["text"].startswith("Shadow Paris lives")
    assert load_prefs()["recent_projects"][0]["path"] == str(repo)
    phrases = [event["phrase"] for event in events]
    assert phrases[0] == "Looking for projects matching “paris shadow”"
    assert "Opening ~/projects/prototypes" in phrases


def test_question_without_a_project_that_opens_nothing_returns_no_turns(monkeypatch, home):
    _use_hosted(monkeypatch, Script(_reply("Your home folder has a projects folder.")))
    result = chat_request(None, "What is in my home folder?")
    assert result["open_project"] is None
    assert "turns" not in result


def test_stop_reaches_a_question_that_is_running(monkeypatch, home):
    server_holder: dict = {}

    def slow_model(messages, tools):
        server_holder["server"].chat_cancel.wait(5)
        return _reply(calls=[_call("list", {"path": "~"}, "1")])

    _use_hosted(monkeypatch, Script())
    from local_coder.agent import runner

    monkeypatch.setattr(runner, "hosted_chat", lambda cfg, body, *, thinking: slow_model(body["messages"], []))
    out = io.StringIO()
    server = RpcServer(out=out)
    server_holder["server"] = server
    lines = [
        json.dumps({"id": "q", "method": "chat_request", "params": {"text": "What is in ~?"}}),
        json.dumps({"id": "s", "method": "cancel", "params": {}}),
    ]
    server.serve(io.StringIO("\n".join(lines) + "\n"))
    assert server._chat_thread is not None
    server._chat_thread.join(10)
    replies = [json.loads(row) for row in out.getvalue().splitlines() if row.strip()]
    answers = {row["id"]: row for row in replies if row.get("id")}
    assert answers["s"]["ok"] is True
    assert answers["q"]["result"]["status"] == "stopped"
    order = [row.get("id") for row in replies if row.get("id")]
    assert order.index("s") < order.index("q")


def _new_project(home: Path, name: str) -> Path:
    folder = home / "Local Coder Projects" / name
    folder.mkdir(parents=True)
    return folder


def test_change_request_in_a_web_app_is_checked_and_reviewable(monkeypatch, home):
    folder = _new_project(home, "board")
    script = Script(
        _reply(calls=[_call("edit", {
            "path": "src/App.tsx",
            "old_string": STARTER_BLOCK,
            "new_string": "<h1>Tasks</h1>",
        }, "1")]),
        _reply("I replaced the starter text with a Tasks heading."),
    )
    review = run_general_request(folder, "Make the page show Tasks", agent_chat=script, start_preview_after=False)
    assert review["engine"] == "agent"
    assert "src/App.tsx" in review["facts"]["files"]
    assert review["model_commentary"] == "I replaced the starter text with a Tasks heading."
    assert review["can_keep"] is True
    meta = load_meta(review["project_id"], review["request_id"])
    assert meta["stop_reason"] == "checks_passed"
    assert meta["agent"]["changed"] == ["src/App.tsx"]
    assert "Tasks" not in (folder / "src" / "App.tsx").read_text()


def test_failed_checks_go_back_to_the_agent(monkeypatch, home):
    folder = _new_project(home, "broken")
    starter_text = STARTER_BLOCK
    script = Script(
        _reply(calls=[_call("edit", {"path": "src/App.tsx", "old_string": starter_text,
                                     "new_string": "<h1>Tasks</h1>"}, "1"),
                      _call("edit", {"path": "src/App.tsx", "old_string": 'import "./App.css";',
                                     "new_string": 'import "./App.css";\nimport Missing from "./Missing";'}, "2")]),
        _reply("Done."),
        _reply(calls=[_call("edit", {"path": "src/App.tsx",
                                     "old_string": '\nimport Missing from "./Missing";', "new_string": ""}, "3")]),
        _reply("I removed the broken import; the check passes now."),
    )
    review = run_general_request(folder, "Show Tasks", agent_chat=script, start_preview_after=False)
    feedback = script.sent[2][-1]["content"]
    assert feedback.startswith("Local Coder checked the working copy")
    assert "Missing" in feedback
    meta = load_meta(review["project_id"], review["request_id"])
    assert meta["stop_reason"] == "checks_passed"
    assert meta["agent"]["repair_rounds"] == 1
    assert review["model_commentary"] == "I removed the broken import; the check passes now."


def test_change_request_outside_web_apps_skips_local_checks(monkeypatch, home):
    folder = home / "projects" / "tool"
    folder.mkdir(parents=True)
    (folder / "pyproject.toml").write_text('[project]\nname = "tool"\n', encoding="utf-8")
    (folder / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    script = Script(
        _reply(calls=[_call("write", {"path": str(folder / "tool.py"), "content": "print('hello')\n"}, "1")]),
        _reply("I changed the greeting."),
    )
    review = run_general_request(folder, "Change the greeting", agent_chat=script, start_preview_after=False)
    assert review["facts"]["files"] == ["tool.py"]
    meta = load_meta(review["project_id"], review["request_id"])
    assert meta["verification"]["local_checks"] == "not_run"
    assert (folder / "tool.py").read_text() == "print('hi')\n"


def test_question_in_a_change_request_answers_without_changes(monkeypatch, home):
    folder = home / "projects" / "prototypes"
    script = Script(_reply("It is a Next.js host for prototypes; nothing needed changing."))
    review = run_general_request(folder, "Fix whatever is wrong if anything", agent_chat=script,
                                 start_preview_after=False)
    assert review["facts"]["files"] == []
    assert review["summary"] == "It is a Next.js host for prototypes; nothing needed changing."
    assert review["can_keep"] is False
    assert load_chat(folder)["turns"][-1]["text"].startswith("It is a Next.js host")


def test_stopping_a_change_request_keeps_the_partial_result_reviewable(monkeypatch, home):
    folder = home / "projects" / "tool2"
    folder.mkdir(parents=True)
    (folder / "pyproject.toml").write_text('[project]\nname = "tool2"\n', encoding="utf-8")
    cancel = threading.Event()

    def model(messages, tools):
        cancel.set()
        return _reply(calls=[_call("write", {"path": "new.py", "content": "x = 1\n"}, "1")])

    review = run_general_request(folder, "Add new.py", agent_chat=model, cancel_event=cancel,
                                 start_preview_after=False)
    meta = load_meta(review["project_id"], review["request_id"])
    assert meta["stop_reason"] == "cancelled"
    assert meta["model_commentary"].startswith("Stopped.")


def test_question_opens_the_repo_it_read_when_the_model_forgot(monkeypatch, home):
    repo = home / "projects" / "prototypes"
    (repo / ".git").mkdir()
    for name in ("a", "b", "c"):
        (repo / "app" / f"{name}.ts").write_text(f"export const {name} = 1\n", encoding="utf-8")
    script = Script(
        _reply(calls=[_call("read", {"path": str(repo / "app" / f"{name}.ts")}, name) for name in ("a", "b", "c")]),
        _reply("Three small modules."),
    )
    _use_hosted(monkeypatch, script)
    result = chat_request(None, "What is in the prototypes app folder?")
    assert result["open_project"] == str(repo)
    assert load_chat(repo)["turns"][-1]["text"] == "Three small modules."
