"""Welcome chat creates a project folder so the user does not pick one first."""
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppError, start_chat
from local_coder.general.starter import allocate_project_name, project_name_from_request


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    return home


def test_project_name_from_request_uses_the_goal():
    assert project_name_from_request("Build a task board") == "task-board"
    assert project_name_from_request("  hello  ") == "hello"
    assert project_name_from_request("???") == "new-project"


def test_allocate_project_name_avoids_occupied_folders(tmp_path):
    parent = tmp_path / "projects"
    (parent / "task-board").mkdir(parents=True)
    (parent / "task-board" / "readme.txt").write_text("taken\n", encoding="utf-8")
    assert allocate_project_name(parent, "task-board") == "task-board-2"


def test_start_chat_creates_an_empty_folder_under_chosen_parent(config_home, tmp_path):
    parent = tmp_path / "Local Coder Projects"
    result = start_chat("Build a task board", parent=parent)
    dest = Path(result["path"])
    assert dest.parent == parent
    assert dest.name == "task-board"
    assert dest.is_dir() and sorted(dest.rglob("*")) == [], "the starter waits in the first request's working copy"
    assert result["inspection"]["exists"] is True and result["inspection"]["verdict"] == "empty"
    assert "Created task-board" in result["detail"]


def test_start_chat_refuses_empty_text(config_home, tmp_path):
    with pytest.raises(AppError, match="Write what you would like"):
        start_chat("   ", parent=tmp_path)


def test_rpc_start_chat(config_home, tmp_path):
    server = RpcServer()
    reply = server.dispatch({
        "id": "1",
        "method": "start_chat",
        "params": {"text": "Make a notes app", "parent": str(tmp_path)},
    })
    assert reply["ok"] is True
    assert Path(reply["result"]["path"]).is_dir()
    assert sorted(Path(reply["result"]["path"]).rglob("*")) == [], "an empty folder: Keep writes the app later"
    assert reply["result"]["inspection"]["verdict"] == "empty"
