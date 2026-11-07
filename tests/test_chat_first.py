"""Chat first: a message that is not plainly work is answered like a normal chat, whose model starts a change when
the message needs one (Nico, 2026-11-07: "just act like a normal LLM and then if needs be start coding")."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.agent.access import AccessPolicy
from local_coder.agent.prompts import ask_prompt
from local_coder.agent.tools import ToolBox
from local_coder.app_service import chat_request
from local_coder.general.chat_log import append_free_turn, load_chat, load_free_chat
from local_coder.general.coordinator import load_meta, run_general_request

HOSTED = machine.MachineConfig(
    model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b", endpoint="https://integrate.api.nvidia.com",
    provider="nvidia", api_key_name="NVIDIA_API_KEY", n_ctx=131072, max_tokens=16384, quantization="hosted",
)


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    home = Path(os.path.realpath(tmp_path / "home"))
    folder = home / "projects" / "paris-map"
    (folder / "src").mkdir(parents=True)
    (folder / "package.json").write_text('{"name": "paris-map"}', encoding="utf-8")
    (folder / "src" / "App.tsx").write_text("export const title = 'Paris';\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    machine.write_machine_config(HOSTED)
    return folder


def _model(monkeypatch, *replies: dict) -> list[list[dict]]:
    from local_coder.agent import runner

    queue, sent = list(replies), []

    def chat(cfg, body, *, thinking, **_):
        sent.append(body)
        return queue.pop(0)

    monkeypatch.setattr(runner, "hosted_chat", chat)
    return sent


def test_a_report_that_needs_work_is_handed_to_a_change_run(project, monkeypatch):
    sent = _model(monkeypatch,
                  _reply(calls=[_call("read", {"path": "src/App.tsx"}, "r")]),
                  _reply("The map never loads its tiles; fixing it now.", [_call("start_change", {}, "s")]))
    result = chat_request(project, "it keeps loading but nothing happens")
    assert result["start_change"] is True and result["status"] == "handoff"
    assert result["summary"] == "The map never loads its tiles; fixing it now."
    assert load_chat(project)["turns"] == [], "the change run keeps the message, with its answer"
    assert "start_change" in [tool["function"]["name"] for tool in sent[0]["tools"]]
    assert "call start_change" in sent[0]["messages"][0]["content"]


def test_a_question_is_answered_and_kept_like_any_chat(project, monkeypatch):
    # The first answer comes without a look, so Nicoder asks once to check; with nothing to check it stands.
    _model(monkeypatch, _reply("The map shows Paris incidents from the last week."), _reply("Nothing to check."))
    result = chat_request(project, "what does the map show?")
    assert "start_change" not in result and result["summary"] == "The map shows Paris incidents from the last week."
    assert [turn["role"] for turn in load_chat(project)["turns"]] == ["user", "assistant"]


def test_a_chat_without_a_project_hands_work_on_and_keeps_no_half_turn(project, monkeypatch):
    append_free_turn("chat-bakery01", "user", "I sell bread in Bandol")
    append_free_turn("chat-bakery01", "assistant", "Nice. What do you need?")
    _model(monkeypatch, _reply(calls=[_call("start_change", {}, "s")]))
    result = chat_request(None, "I need a website for my bakery", chat_id="chat-bakery01")
    assert result["start_change"] is True and result["chat_id"] == "chat-bakery01"
    assert [turn["text"] for turn in load_free_chat("chat-bakery01")["turns"]] == [
        "I sell bread in Bandol", "Nice. What do you need?"], "the new project's run keeps the message"


def test_start_change_is_offered_only_to_the_apps_chat(tmp_path):
    box = ToolBox(AccessPolicy(home=tmp_path), cwd=tmp_path, run_tmp=tmp_path / "tmp")
    assert "start_change" not in box.names
    assert "start_change" not in ask_prompt(home=Path("/Users/n"), project=None)
    assert "start_change" in ask_prompt(home=Path("/Users/n"), project=None, can_change=True)


def test_a_handed_over_message_is_changed_even_when_it_reads_as_a_question(project):
    def engine(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "App.tsx").write_text("export const title = 'Paris live';\n", encoding="utf-8")

    review = run_general_request(project, "why is the title wrong? it should say Paris live", request_id="h1",
                                 fake_engine=engine, start_preview_after=False, handoff=True)
    candidate = Path(load_meta(review["project_id"], "h1")["candidate"])
    assert "Paris live" in (candidate / "src" / "App.tsx").read_text(encoding="utf-8")
    assert [turn["role"] for turn in load_chat(project)["turns"]] == ["user", "assistant"]
