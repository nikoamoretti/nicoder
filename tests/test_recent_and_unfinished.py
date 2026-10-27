"""The project list keeps its order under the pointer, and unfinished work says so in plain words.

Nico (2026-10-26): "chats have no date and order so when i click on one it becomes the first in the list which is
very confusing", and a crime-data spec that ran out of steps showed "the in-app preview does not run" instead of
what the agent had built.
"""
from __future__ import annotations

import time

from local_coder import machine
from local_coder.app_service import load_prefs, remember_project
from local_coder.general.coordinator import _preview_failed_summary, unfinished_text


def _names() -> list[str]:
    return [row["name"] for row in load_prefs()["recent_projects"]]


def test_clicking_a_project_does_not_reorder_the_list(tmp_path, monkeypatch):
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(tmp_path / "config"))
    folders = []
    for name in ("alpha", "beta", "gamma"):
        folder = tmp_path / name
        folder.mkdir()
        folders.append(folder)
        remember_project(folder)
        time.sleep(0.01)
    assert _names() == ["gamma", "beta", "alpha"]
    remember_project(folders[0], active=False)  # opened / clicked
    assert _names() == ["gamma", "beta", "alpha"]
    remember_project(folders[0])  # a request or chat ran in it
    assert _names() == ["alpha", "gamma", "beta"]
    assert all(isinstance(row.get("active"), float) for row in load_prefs()["recent_projects"])


def test_a_new_project_opened_without_activity_still_joins_the_list(tmp_path, monkeypatch):
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(tmp_path / "config"))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    remember_project(first)
    remember_project(second, active=False)
    assert set(_names()) == {"first", "second"}


def test_unfinished_work_keeps_the_agents_account_and_says_how_to_go_on():
    text = unfinished_text("I built the ingestion layer in src/ingestion.", "step_limit")
    assert text.startswith("I built the ingestion layer")
    assert text.endswith('I ran out of steps before finishing. Say "continue" and I\'ll pick up where I left off.')
    assert "preview" not in text
    assert unfinished_text("", "time_limit").startswith("I ran out of time")


def test_the_failure_note_reads_as_sentences():
    note = _preview_failed_summary("the preview is still the empty starter page", ["a.ts"] * 11)
    assert note == ("I wrote 11 files, but the app does not show it yet. The preview is still the empty starter page. "
                    "Your project is unchanged: keep these files or discard them.")


def test_a_run_that_hits_the_step_limit_reports_its_work_not_a_preview_error(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from local_coder.general import coordinator
    from local_coder.general.coordinator import load_meta, run_general_request
    from local_coder.general.starter import copy_starter

    from .test_agent_app import Script, _call, _reply

    home = Path(os.path.realpath(tmp_path / "home"))
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(coordinator, "AGENT_MAX_WALL_S", 0)  # no time left to carry on by itself: it stops here
    monkeypatch.setattr(coordinator, "AGENT_MAX_STEPS", 3)
    folder = home / "Local Coder Projects" / "crime-map"
    folder.mkdir(parents=True)
    copy_starter(folder)
    writes = [
        _reply(calls=[_call("write", {"path": f"src/ingestion/part{n}.ts", "content": f"export const p{n} = {n};\n"},
                            str(n))])
        for n in range(8)
    ]
    script = Script(*writes, _reply("I built three ingestion modules; the page is next."))
    review = run_general_request(folder, "Scrape the Interstat datasets", agent_chat=script, start_preview_after=False)
    meta = load_meta(review["project_id"], review["request_id"])
    assert meta["stop_reason"] == "step_limit"
    assert meta["model_commentary"].endswith("Say \"continue\" and I'll pick up where I left off.")
    assert "preview" not in meta["model_commentary"]
    assert meta["verification"]["local_checks"] == "not_run"
    assert review["can_keep"] is True  # the work done so far can still be kept


def test_projects_saved_before_dates_get_theirs_from_their_records(tmp_path, monkeypatch):
    # Nico (2026-10-26): "chats have no date and order". Rows saved before activity times showed no date.
    import json as _json

    from local_coder import machine
    from local_coder.app_service import dated_prefs, save_prefs
    from local_coder.general.chat_log import save_chat

    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    talked, idle = tmp_path / "talked", tmp_path / "idle"
    talked.mkdir()
    idle.mkdir()
    save_chat(talked, [{"id": "t1", "role": "user", "text": "hi", "ts": 2_000_000_000.0}])
    save_prefs({"recent_projects": [{"path": str(idle), "name": "idle"}, {"path": str(talked), "name": "talked"}],
                "profile": "d2", "engine": "general"})

    rows = dated_prefs()["recent_projects"]

    assert [row["name"] for row in rows] == ["talked", "idle"]  # newest activity first
    assert rows[0]["active"] == 2_000_000_000.0
    assert rows[1]["active"] == idle.stat().st_mtime  # no records: the folder's last change
    saved = _json.loads((tmp_path / "config" / "app.json").read_text())
    assert all("active" in row for row in saved["recent_projects"])  # filled once, kept
