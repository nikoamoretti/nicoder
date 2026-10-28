"""Each chat and project keeps its own model; the last pick is what new conversations start with."""
from __future__ import annotations

import threading

import pytest

from local_coder import app_service, machine
from local_coder.app_service import (
    chat_request,
    conversation_config,
    conversation_key,
    conversation_model,
    conversation_model_for,
    load_free_chat_payload,
    load_project_chat,
    select_model,
    set_reasoning,
)
from local_coder.general.chat_log import append_free_turn

SONNET = "orca:anthropic/claude-sonnet-5.5"
KIMI = "orca:kimi/kimi-k2.7-code"
QWEN = "orca:qwen/qwen3.8-27b"


@pytest.fixture(autouse=True)
def orcarouter_key(monkeypatch):
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-orca-TestKey1234567890abcdef")
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True})


def _alias() -> str:
    return machine.load_machine_config().model_alias


def test_each_chat_runs_on_the_model_picked_in_it():
    select_model(SONNET, chat_id="chat-a")
    select_model(KIMI, chat_id="chat-b")
    assert _alias() == "kimi/kimi-k2.7-code", "the last pick is the machine's model"
    with conversation_model_for(chat_id="chat-a") as chosen:
        assert (chosen, _alias()) == (SONNET, "anthropic/claude-sonnet-5.5")
        assert machine.load_machine_config().provider == "orcarouter"
    assert _alias() == "kimi/kimi-k2.7-code", "the chat's model held for that request only"
    with conversation_model_for(chat_id="chat-b") as chosen:
        assert (chosen, _alias()) == (KIMI, "kimi/kimi-k2.7-code")
    with conversation_model_for(chat_id="chat-new") as chosen:
        assert chosen == KIMI, "a new chat starts with the last pick"
    assert conversation_model("chat:chat-new") == KIMI, "and keeps it from then on"
    assert machine.saved_machine_config().model_alias == "kimi/kimi-k2.7-code"


def test_picking_a_model_in_one_chat_leaves_the_others_on_theirs(tmp_path):
    project = tmp_path / "site"
    project.mkdir()
    app_service.remember_project(project)
    append_free_turn("chat-old", "user", "hello")
    select_model(QWEN)
    select_model(SONNET, chat_id="chat-new")
    assert load_free_chat_payload("chat-old")["model"] == QWEN
    assert load_project_chat(project)["model"] == QWEN
    assert load_free_chat_payload("chat-new")["model"] == SONNET
    with conversation_model_for(project) as chosen:
        assert (chosen, _alias()) == (QWEN, "qwen/qwen3.8-27b")


def test_two_chats_run_two_models_at_once():
    select_model(SONNET, chat_id="chat-a")
    select_model(KIMI, chat_id="chat-b")
    both = threading.Barrier(2)
    seen: dict[str, str] = {}

    def run(chat_id: str) -> None:
        with conversation_model_for(chat_id=chat_id):
            both.wait(timeout=5)
            seen[chat_id] = _alias()
            both.wait(timeout=5)

    threads = [threading.Thread(target=run, args=(chat,)) for chat in ("chat-a", "chat-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert seen == {"chat-a": "anthropic/claude-sonnet-5.5", "chat-b": "kimi/kimi-k2.7-code"}


def test_a_request_never_saves_its_own_model():
    select_model(SONNET, chat_id="chat-a")
    select_model(KIMI, chat_id="chat-b")
    with conversation_model_for(chat_id="chat-a"):
        cfg = machine.load_machine_config()
        machine.write_machine_config(cfg.model_copy(update={"mac_access": False}))
    saved = machine.saved_machine_config()
    assert (saved.model_alias, saved.mac_access) == ("kimi/kimi-k2.7-code", False)


def test_a_question_runs_on_its_chats_model(monkeypatch):
    from local_coder.agent import runner

    sent: list[str] = []

    def model(cfg, body, *, thinking, **_):
        sent.append(cfg.model_alias)
        answer = {"role": "assistant", "content": "It is a tiny site."}
        return {"choices": [{"message": answer, "finish_reason": "stop"}]}

    monkeypatch.setattr(runner, "hosted_chat", model)
    select_model(SONNET, chat_id="chat-a")
    select_model(KIMI, chat_id="chat-b")
    assert chat_request(None, "What is in my home folder?", chat_id="chat-a")["summary"] == "It is a tiny site."
    assert set(sent) == {"anthropic/claude-sonnet-5.5"}


def test_reasoning_is_switched_for_the_open_conversations_model():
    select_model(QWEN, chat_id="chat-a")
    select_model(KIMI, chat_id="chat-b")
    assert set_reasoning("off", QWEN)["model"] == QWEN
    assert machine.saved_machine_config().reasoning == "on", "the machine's own model keeps its setting"
    assert load_free_chat_payload("chat-a")["reasoning"] == "off"
    with conversation_model_for(chat_id="chat-a"):
        assert machine.load_machine_config().reasoning == "off"


def test_a_missing_key_stops_only_that_chat(monkeypatch):
    select_model(SONNET, chat_id="chat-a")
    monkeypatch.setattr(app_service, "machine_model_id", lambda: "nemotron35-super")
    monkeypatch.delenv("ORCAROUTER_API_KEY")
    with pytest.raises(app_service.AppError, match="Connect OrcaRouter"):
        with conversation_model_for(chat_id="chat-a"):
            pass


def test_the_projects_model_decides_if_the_agent_can_look(tmp_path):
    project = tmp_path / "site"
    project.mkdir()
    select_model(SONNET, path=project)
    select_model(KIMI, chat_id="chat-b")
    assert conversation_config(project).model_alias == "anthropic/claude-sonnet-5.5"
    assert conversation_key(project) == f"project:{project.resolve()}"
