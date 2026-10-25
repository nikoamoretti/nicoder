"""With no project open, a message is a chat unless it asks to build something new.

Nico's welcome chat (2026-10-22): "tell about australopithecus" made a project folder with the React starter
in it, because outside a project every unclear message was treated as a change.
"""
from __future__ import annotations

import io

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime
from local_coder.general.inspect import classify_without_project

CHATS = [
    "tell about australopithecus",
    "tell me about australopithecus",
    "hi",
    "hello, how are you",
    "help me plan a trip to Lisbon",
    "summarize my week",
    "find where shadow paris gets its incident data",
    "add a startup.jobs ingestor to vc-jobs-board",
    "make the toggle remember my choice",
    "what is in my app-scout repo?",
]
BUILDS = [
    "build a todo app",
    "write me a python script that renames my photos",
    "create a landing page for my bakery",
    "I need a website for my bakery",
    "make a game where a frog jumps over cars",
    "start a new project for my recipes",
]


@pytest.mark.parametrize("text", CHATS)
def test_without_a_project_a_message_is_a_chat(text):
    assert classify_without_project(text)["kind"] == "chat"


@pytest.mark.parametrize("text", BUILDS)
def test_without_a_project_only_a_new_build_makes_a_project(text):
    assert classify_without_project(text)["kind"] == "code"


def test_the_app_asks_without_a_path_and_gets_a_chat():
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    reply = server.dispatch({"id": "1", "method": "classify_request", "params": {"text": "hi"}})
    assert reply["ok"] is True and reply["result"]["kind"] == "chat"
    reply = server.dispatch({"id": "2", "method": "classify_request", "params": {"text": "build a todo app"}})
    assert reply["result"]["kind"] == "code"


def test_an_empty_message_is_still_empty():
    assert classify_without_project("   ")["kind"] == "empty"
