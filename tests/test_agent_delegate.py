"""Delegated work: read-only exploring inside one project, and drafts that wait for apply or discard."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_coder.agent import delegate


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
        self.tools: list[list[str]] = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        self.sent.append(json.loads(json.dumps(messages)))
        self.tools.append([tool["function"]["name"] for tool in tools])
        return self.replies.pop(0)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    app = root / "projects" / "app"
    (app / "src").mkdir(parents=True)
    (app / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    (app / "src" / "price.ts").write_text("export const price = 10;\n", encoding="utf-8")
    other = root / "projects" / "other"
    other.mkdir(parents=True)
    (other / "notes.md").write_text("private notes\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(root))
    return root


def test_explore_stays_inside_the_project(home):
    app = home / "projects" / "app"
    script = Script(
        _reply(calls=[_call("read", {"path": "src/price.ts"}, "1"),
                      _call("read", {"path": str(home / "projects" / "other" / "notes.md")}, "2")]),
        _reply("The price is 10 (src/price.ts:1)."),
    )
    result = delegate.explore("What is the price?", project=str(app), chat=script, home=home)
    assert result["answer"] == "The price is 10 (src/price.ts:1)."
    assert result["files_read"] == ["~/projects/app/src/price.ts"]
    refused = script.sent[1][-1]["content"]
    assert "the folder this task is limited to" in refused
    assert "find_project" not in script.tools[0] and "open_project" not in script.tools[0]
    assert "write" not in script.tools[0]


def test_explore_without_a_project_can_find_one(home):
    script = Script(_reply("Nothing to look at."))
    delegate.explore("Which project prices things?", chat=script, home=home)
    assert script.tools[0][0] == "find_project"


@pytest.mark.parametrize("raw,needle", [
    ("~/projects/missing", "is not a folder"),
    ("/", "must be a project folder inside"),
])
def test_explore_refuses_bad_projects(home, raw, needle):
    path = raw.replace("~", str(home))
    with pytest.raises(delegate.DelegateError, match=needle):
        delegate.explore("x", project=path, chat=Script(), home=home)


def test_draft_changes_only_the_copy_until_applied(home):
    app = home / "projects" / "app"
    (app / "node_modules" / "dep").mkdir(parents=True)
    (app / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    script = Script(
        _reply(calls=[_call("edit", {"path": str(app / "src" / "price.ts"), "old_string": "10", "new_string": "12"},
                            "1")]),
        _reply("Changed the price to 12 (src/price.ts:1)."),
    )
    draft = delegate.create_draft(str(app), "Set the price to 12.", chat=script, home=home)
    assert draft["files"] == ["src/price.ts"]
    assert "-export const price = 10;" in draft["diff"] and "+export const price = 12;" in draft["diff"]
    assert draft["state"] == "open"
    assert (app / "src" / "price.ts").read_text() == "export const price = 10;\n"
    assert (Path(draft["work_folder"]) / "node_modules" / "dep" / "index.js").is_file()
    assert "find_project" not in script.tools[0] and "write" in script.tools[0]
    applied = delegate.apply_draft(draft["draft_id"])
    assert applied == {"applied": True, "project": str(app), "files": ["src/price.ts"]}
    assert (app / "src" / "price.ts").read_text() == "export const price = 12;\n"
    with pytest.raises(delegate.DelegateError, match="does not exist"):
        delegate.apply_draft(draft["draft_id"])


def test_apply_refuses_when_the_project_changed_meanwhile(home):
    app = home / "projects" / "app"
    script = Script(
        _reply(calls=[_call("edit", {"path": "src/price.ts", "old_string": "10", "new_string": "12"}, "1")]),
        _reply("Done."),
    )
    draft = delegate.create_draft(str(app), "Set the price to 12.", chat=script, home=home)
    (app / "src" / "price.ts").write_text("export const price = 11;\n", encoding="utf-8")
    result = delegate.apply_draft(draft["draft_id"])
    assert result["applied"] is False and result["code"] == "conflict"
    assert (app / "src" / "price.ts").read_text() == "export const price = 11;\n"
    assert delegate.discard_draft(draft["draft_id"]) == {"discarded": True, "draft_id": draft["draft_id"]}


def test_an_empty_draft_cannot_be_applied(home):
    script = Script(_reply("No change needed."))
    draft = delegate.create_draft(str(home / "projects" / "app"), "Look only.", chat=script, home=home)
    assert draft["state"] == "empty" and draft["files"] == []
    with pytest.raises(delegate.DelegateError, match="nothing to apply"):
        delegate.apply_draft(draft["draft_id"])


def test_drafts_refuse_folders_that_are_not_projects_or_too_big(home, monkeypatch):
    with pytest.raises(delegate.DelegateError, match="does not look like a project"):
        delegate.create_draft(str(home / "projects" / "other"), "x", chat=Script(), home=home)
    monkeypatch.setattr(delegate, "MAX_PROJECT_FILES", 1)
    with pytest.raises(delegate.DelegateError, match="too large"):
        delegate.create_draft(str(home / "projects" / "app"), "x", chat=Script(), home=home)


def test_draft_ids_are_validated():
    with pytest.raises(delegate.DelegateError, match="not a draft id"):
        delegate.discard_draft("../../etc")
