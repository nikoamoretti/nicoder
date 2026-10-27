"""With no project open: a request to do something is built, a question is answered.

Nico's welcome chat (2026-10-22): "tell about australopithecus" made a project folder. Then (2026-10-25) a
spec he sent with "do this" was treated as a question, and the chat went looking for a matching folder
("super dumb"). Work starts a new project at once, or runs in the project it names; questions stay a chat.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime
from local_coder.general.inspect import classify_without_project

SPEC = (
    "do this * Data Ingestion: Scrape and ingest the Interstat datasets, which provide annual and monthly crime "
    "figures localized by commune or département. * Geographic Profiling: Use a Rossmo's Formula implementation "
    "to map the hunting grounds of specific crime types."
)
CHATS = [
    "tell about australopithecus",
    "tell me about australopithecus",
    "hi",
    "hello, how are you",
    "help me plan a trip to Lisbon",
    "summarize my week",
    "find where shadow paris gets its incident data",
    "what is in my app-scout repo?",
    "can you explain the scraper?",
    "is there a way to make it faster",
]
NEW_WORK = [
    SPEC,
    "build a todo app",
    "write me a python script that renames my photos",
    "create a landing page for my bakery",
    "scrape the Interstat datasets and map crime by commune",
    "can you build a todo app?",
    "please make a game where a frog jumps over cars",
    "I need a website for my bakery",
    "make a task board app",
]
PROJECTS = [Path("/Users/n/projects/vc-jobs-board"), Path("/Users/n/projects/prototypes"),
            Path("/Users/n/projects/task-board")]


@pytest.mark.parametrize("text", CHATS)
def test_a_question_without_a_project_is_a_chat(text):
    assert classify_without_project(text, PROJECTS)["kind"] == "chat"


@pytest.mark.parametrize("text", NEW_WORK)
def test_work_without_a_project_starts_a_new_one(text):
    verdict = classify_without_project(text, PROJECTS)
    assert verdict["kind"] == "code"
    assert "project" not in verdict


@pytest.mark.parametrize(("text", "name"), [
    ("add a startup.jobs ingestor to vc-jobs-board", "vc-jobs-board"),
    ("fix the login page in my prototypes", "prototypes"),
    ("do this for the vc jobs board: dedupe the listings", "vc-jobs-board"),
])
def test_work_that_names_a_project_runs_there(text, name):
    verdict = classify_without_project(text, PROJECTS)
    assert verdict["kind"] == "code"
    assert Path(verdict["project"]).name == name


def test_the_app_asks_without_a_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "projects" / "vc-jobs-board").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())

    def ask(text):
        return server.dispatch({"id": "1", "method": "classify_request", "params": {"text": text}})["result"]

    assert ask("hi")["kind"] == "chat"
    assert ask(SPEC)["kind"] == "code" and "project" not in ask(SPEC)
    assert Path(ask("add a startup.jobs ingestor to vc-jobs-board")["project"]).name == "vc-jobs-board"


def test_an_empty_message_is_still_empty():
    assert classify_without_project("   ")["kind"] == "empty"


def test_solving_is_work_even_with_a_question_in_it():
    assert classify_without_project("Solve the zebra puzzle: who owns the zebra?")["kind"] == "code"
    assert classify_without_project("how does the router work?")["kind"] == "chat"
