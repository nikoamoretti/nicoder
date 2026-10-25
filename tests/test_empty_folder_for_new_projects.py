"""Cut starter-in-candidate, item 4: New Project and the welcome chat make an empty folder, and nothing in it.

A request already puts the starter into its working copy (`test_starter_in_candidate.py`). The two places that make
folders used to copy the starter into them as well, so a new project held twelve files before the first request. They
make the folder and nothing else now: the folder has to exist (Keep never makes folders), and everything that goes
inside waits for Keep.

A Finder `.DS_Store` is not content. A folder he has looked at in Finder holds one, and counting it would silently
skip the starter for a folder that looks empty to him. Everything else hidden (`.git`) is his.

Fake engines and a scripted hosted model stand in for the models ($0). Each test names the acceptance check it
belongs to (SC: a gap the cut closes, SG: a guard that must not move, SR: reported behaviour that is pinned).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import discard_changes, keep_changes, load_prefs, start_chat
from local_coder.general.coordinator import list_requests, load_meta, run_general_request
from local_coder.general.inspect import inspect_project
from local_coder.general.snapshot import index_tree
from local_coder.general.starter import allocate_project_name, create_project, starter_root

OLD_APP = "<h1>New app</h1>\n      <p>This starter is empty on purpose. Describe what you want to build.</p>"
STARTER_FILES = sorted(index_tree(starter_root()).files)
WANT = sorted({*STARTER_FILES, "src/Tasks.tsx"})
FINDER_NOISE = b"\x00\x00\x00\x01Bud1"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME with the data dir and the projects folder inside it."""
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(root / ".local" / "share" / "local-coder"))
    monkeypatch.setenv("LOCAL_CODER_PROJECTS_DIR", str(root / "Local Coder Projects"))
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(root / ".config" / "local-coder"))
    return root


def entries(root: Path) -> list[str]:
    """Everything under root, hidden files and folders included."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def files_in(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def rpc(method: str, **params) -> dict:
    reply = RpcServer().dispatch({"id": "1", "method": method, "params": params})
    return reply["result"] if reply.get("ok") else {"ERROR": reply.get("error")}


def tasks(cwd: Path) -> None:
    """The model replaces the starter's text and adds a file."""
    app = cwd / "src" / "App.tsx"
    app.write_text(app.read_text(encoding="utf-8").replace(OLD_APP, "<h1>Tasks</h1>"), encoding="utf-8")
    (cwd / "src" / "Tasks.tsx").write_text("export const tasks = ['milk'];\n", encoding="utf-8")


def nothing(_cwd: Path) -> None:
    return None


def local(project: Path, request_id: str, engine) -> dict:
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd),
    )


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A scripted hosted model: it answers with the replies in order and keeps what it was sent."""

    def __init__(self, *replies: dict) -> None:
        self.replies, self.sent = list(replies), []

    def __call__(self, messages: list[dict], _tools: list[dict]) -> dict:
        self.sent.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0)


# === Check 8: New Project and the welcome chat (SC9, SC10, SG3, SR1) ============================================


def test_new_project_makes_an_empty_folder_and_remembers_it(home):
    parent = home / "Local Coder Projects"
    made = rpc("new_project", parent=str(parent), name="fresh")
    dest = Path(made["path"])
    assert dest.is_dir() and entries(dest) == [], "nothing in it, not even the starter"
    inspection = made["inspection"]
    assert inspection["verdict"] == "empty" and inspection["supported"] is True and inspection["can_create"] is True
    assert inspection["has_package_json"] is False
    assert any(row["path"] == str(dest.resolve()) for row in load_prefs()["recent_projects"])
    assert "ERROR" not in rpc("new_project", parent=str(parent), name="fresh"), "an empty folder is accepted again"


def test_new_project_still_refuses_a_folder_with_files(home):
    parent = home / "Local Coder Projects"
    full = parent / "full"
    full.mkdir(parents=True)
    (full / "a.txt").write_text("a\n", encoding="utf-8")
    assert "ERROR" in rpc("new_project", parent=str(parent), name="full")
    assert files_in(full) == ["a.txt"]


def test_the_welcome_chat_makes_an_empty_folder_and_its_first_request_builds_the_starter_in_the_copy(home):
    made = rpc("start_chat", text="Build a task board")
    dest = Path(made["path"])
    assert dest.parent == (home / "Local Coder Projects") and dest.name == "task-board"
    assert dest.is_dir() and entries(dest) == []
    assert made["detail"].startswith("Created task-board in ")
    assert made["inspection"]["verdict"] == "empty"
    review = local(dest, "r1", tasks)
    assert entries(dest) == [], "the folder stays empty until Keep"
    assert review["facts"]["files"] == WANT
    assert keep_changes("r1", dest)["applied"] == WANT and files_in(dest) == WANT


def test_the_helpers_replies_keep_the_keys_the_mac_app_reads(home):
    made = rpc("new_project", parent=str(home / "Local Coder Projects"), name="keys")
    assert {"path", "inspection"} <= set(made)
    chat = rpc("start_chat", text="Build a notes app")
    assert {"path", "name", "inspection", "detail"} <= set(chat)
    dest = Path(made["path"])
    review = local(dest, "r1", tasks)
    assert {"summary", "can_keep", "can_discard", "facts", "diff", "episode_id", "cost_usd", "outcome", "blocked_by",
            "verification"} <= set(rpc("snapshot", episode_id="r1", path=str(dest)))
    assert review["can_keep"] is True
    assert {"ok", "applied", "also_kept"} <= set(rpc("keep", episode_id="r1", path=str(dest)))
    rows = list_requests(dest)
    assert rows and {"episode_id", "outcome", "request"} <= set(rows[0])


def test_the_welcome_chat_reuses_an_empty_folder_but_not_one_that_was_kept(home):
    """Reported (SR1): after a Discard the same sentence reuses the empty folder, while a result waits it does too (the
    sentence becomes a follow-up in that project), and after a Keep the next folder is `-2`."""
    first = Path(rpc("start_chat", text="Build a pomodoro timer")["path"])
    local(first, "r1", tasks)
    assert Path(rpc("start_chat", text="Build a pomodoro timer")["path"]) == first, "a result waits: same empty folder"
    discard_changes("r1", repo=first)
    assert Path(rpc("start_chat", text="Build a pomodoro timer")["path"]) == first
    kept = Path(rpc("start_chat", text="Build a recipe box")["path"])
    local(kept, "r1", tasks)
    keep_changes("r1", kept)
    assert Path(rpc("start_chat", text="Build a recipe box")["path"]).name == "recipe-box-2"


def test_start_chat_and_create_project_write_nothing_into_the_folder_they_make(home):
    parent = home / "projects"
    assert entries(create_project(parent, "plain")) == []
    chat = Path(start_chat("Build a task board", parent=parent)["path"])
    assert entries(chat) == []


# === The Finder's .DS_Store is not content ======================================================================


def test_a_folder_holding_only_a_finder_ds_store_is_empty_for_the_inspection_and_the_names(home):
    parent = home / "projects"
    (parent / "task-board").mkdir(parents=True)
    (parent / "task-board" / ".DS_Store").write_bytes(FINDER_NOISE)
    assert inspect_project(parent / "task-board")["verdict"] == "empty"
    assert allocate_project_name(parent, "task-board") == "task-board", "an empty folder is reused"
    assert create_project(parent, "task-board") == (parent / "task-board").resolve(), "and accepted by New Project"
    (parent / "task-board" / "notes.txt").write_text("his\n", encoding="utf-8")
    assert allocate_project_name(parent, "task-board") == "task-board-2"
    assert inspect_project(parent / "task-board")["verdict"] != "empty"
    with pytest.raises(ValueError):
        create_project(parent, "task-board")


def test_a_first_request_in_a_folder_with_only_a_ds_store_still_gets_the_starter_and_keep_leaves_the_ds_store(home):
    folder = home / "projects" / "board"
    folder.mkdir(parents=True)
    (folder / ".DS_Store").write_bytes(FINDER_NOISE)
    review = local(folder, "r1", tasks)
    assert load_meta(review["project_id"], "r1")["starter_used"] is True
    assert review["facts"]["files"] == WANT, "the review lists the starter and the edit, not the .DS_Store"
    assert ".DS_Store" not in review["facts"]["delta"]["deleted"]
    assert entries(folder) == [".DS_Store"], "nothing else is in his folder before Keep"
    kept = keep_changes("r1", folder)
    assert kept["applied"] == WANT
    assert (folder / ".DS_Store").read_bytes() == FINDER_NOISE
    assert files_in(folder) == sorted([*WANT, ".DS_Store"])


def test_the_hosted_agent_in_a_folder_with_a_ds_store_starts_in_the_starter_and_keeps_the_ds_store(home):
    folder = home / "projects" / "board"
    folder.mkdir(parents=True)
    (folder / ".DS_Store").write_bytes(FINDER_NOISE)
    script = Script(
        reply(calls=[call("read", {"path": "package.json"}, "1")]),
        reply(calls=[call("write", {"path": "src/Tasks.tsx", "content": "export const tasks = [];\n"}, "2")]),
        reply("Added Tasks.tsx."),
    )
    review = run_general_request(folder, "Show Tasks", request_id="r1", agent_chat=script, start_preview_after=False)
    seen = [m.get("content", "") for m in script.sent[1] if m.get("role") == "tool"]
    assert any('"name": "local-coder-app"' in text for text in seen), "the agent read the starter"
    assert review["facts"]["files"] == sorted({*STARTER_FILES, "src/Tasks.tsx"})
    assert review["facts"]["delta"]["deleted"] == [], "the .DS_Store is not deleted by the agent's start"
    assert entries(folder) == [".DS_Store"]


def test_a_first_request_with_a_ds_store_that_changes_nothing_is_still_no_result(home):
    folder = home / "projects" / "board"
    folder.mkdir(parents=True)
    (folder / ".DS_Store").write_bytes(FINDER_NOISE)
    review = local(folder, "r1", nothing)
    assert review["facts"]["files"] == [] and review["can_keep"] is False and review["can_discard"] is False
    assert entries(folder) == [".DS_Store"]


def test_other_hidden_content_still_counts_a_folder_with_a_git_directory_gets_no_starter(home):
    folder = home / "projects" / "repo"
    (folder / ".git").mkdir(parents=True)
    assert inspect_project(folder)["verdict"] != "empty"
    review = local(folder, "r1", lambda cwd: (cwd / "main.py").write_text("print('hi')\n", encoding="utf-8"))
    assert load_meta(review["project_id"], "r1")["starter_used"] is False
    assert review["facts"]["files"] == ["main.py"]
