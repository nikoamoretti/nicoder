"""A change that touches no web files gets no web checks, and the agent's scratch folders never reach a review.

Seen in Nico's own requests (2026-10-23): in a Vite project, a request that wrote a Python scraper was sent back
twice to "fix the empty starter page" and ended "not repaired", and the 696 files the agent had put in
`.agenttmp/` (browsers, a uv cache) were listed as written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_coder.app_service import keep_changes
from local_coder.general.coordinator import _touches_web, load_meta, run_general_request
from local_coder.general.snapshot import is_excluded, read_index
from local_coder.general.starter import copy_starter

from .test_agent_app import Script, _call, _reply


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    return root


def _web_project(home: Path, name: str) -> Path:
    folder = home / "Local Coder Projects" / name
    folder.mkdir(parents=True)
    return copy_starter(folder)


def test_a_python_change_in_a_web_project_runs_no_web_checks(home):
    folder = _web_project(home, "site")
    script = Script(
        _reply(calls=[_call("write", {"path": "scraper/jobs.py", "content": "print('jobs')\n"}, "1")]),
        _reply("I wrote a scraper."),
    )
    review = run_general_request(folder, "Write a jobs scraper", agent_chat=script, start_preview_after=False)
    assert review["facts"]["files"] == ["scraper/jobs.py"]
    assert review["can_keep"] is True
    meta = load_meta(review["project_id"], review["request_id"])
    assert meta["verification"]["local_checks"] == "not_run"
    assert meta["verification"]["detail"].startswith("This change touched no web files")
    assert not meta.get("repair_state")
    assert meta.get("stop_reason") in (None, "")
    assert len(script.sent) == 2  # the change and its answer: no repair round


def test_a_python_change_in_a_new_project_runs_no_web_checks(home):
    folder = home / "Local Coder Projects" / "new"
    folder.mkdir(parents=True)  # empty: the starter goes into the working copy, and its files don't count
    script = Script(
        _reply(calls=[_call("write", {"path": "scraper/jobs.py", "content": "print('jobs')\n"}, "1")]),
        _reply("I wrote a scraper."),
    )
    review = run_general_request(folder, "Write a jobs scraper", agent_chat=script, start_preview_after=False)
    meta = load_meta(review["project_id"], review["request_id"])
    assert "scraper/jobs.py" in review["facts"]["files"]
    assert meta["verification"]["local_checks"] == "not_run"
    assert len(script.sent) == 2


def test_an_index_frozen_before_the_exclusions_drops_them(tmp_path):
    frozen = tmp_path / "baseline.json"
    row = {"sha256": "ab", "mode": 0o100644, "size": 1}
    frozen.write_text(json.dumps({"root": str(tmp_path), "files": {
        ".cache/pip/x": row, ".agenttmp/uv/y": row, "scraper/jobs.py": row}}), encoding="utf-8")
    assert sorted(read_index(frozen).files) == ["scraper/jobs.py"]


def test_a_web_change_is_still_checked(home):
    folder = _web_project(home, "board")
    script = Script(
        _reply(calls=[_call("write", {"path": "src/extra.css", "content": "h1 { color: red; }\n"}, "1")]),
        _reply("Done."),
        _reply("Still done."),
        _reply("Done again."),
    )
    review = run_general_request(folder, "Make the heading red", agent_chat=script, start_preview_after=False)
    meta = load_meta(review["project_id"], review["request_id"])
    # src/ is the web app: the starter's empty page is still found and sent back, as before.
    assert meta["verification"].get("local_checks") != "not_run"
    assert meta["verification"]["preview_health"]["code"] == "empty_page"


def test_the_agents_scratch_folders_never_reach_the_review_or_keep(home):
    folder = _web_project(home, "jobs")
    script = Script(
        _reply(calls=[
            _call("write", {"path": ".agenttmp/uv-cache/blob.txt", "content": "cache\n"}, "1"),
            _call("write", {"path": ".cache/pip/wheel.txt", "content": "cache\n"}, "2"),
            _call("write", {"path": "scraper/jobs.py", "content": "print('jobs')\n"}, "3"),
        ]),
        _reply("Done."),
    )
    review = run_general_request(folder, "Write a jobs scraper", agent_chat=script, start_preview_after=False)
    assert review["facts"]["files"] == ["scraper/jobs.py"]
    keep_changes(review["request_id"], folder)
    assert (folder / "scraper" / "jobs.py").read_text() == "print('jobs')\n"
    assert not (folder / ".agenttmp").exists()
    assert not (folder / ".cache").exists()


def test_scratch_folders_are_excluded_and_web_files_recognised():
    assert is_excluded(".agenttmp/pw-browsers/chromium/x")
    assert is_excluded("tools/.cache/pip/y")
    assert not is_excluded("scraper/jobs.py")
    assert _touches_web(["src/App.tsx"]) and _touches_web(["package.json"]) and _touches_web(["index.html"])
    assert _touches_web(["public/logo.svg"]) and _touches_web(["tsconfig.app.json"])
    assert not _touches_web(["scraper/jobs.py", "README.md", "data/rows.json"])
