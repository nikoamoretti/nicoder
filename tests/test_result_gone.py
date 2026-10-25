"""Cut starter-in-candidate, item 5: Keep refuses a result whose `candidate/` is gone, whatever the baseline lists.

Net-out's guard says "the baseline lists files and the candidate none". A first request on an empty folder now has an
empty baseline, so a `candidate/` that was deleted looked like a result that changed nothing: Keep answered ok with
nothing written and the request read `kept` ("These changes are in your project"). `apply_delta` now refuses a
`candidate/` that is not a folder before any write, for Keep and for the MCP apply alike. It tests the folder and not
"no files": a candidate that exists and is empty still applies when the baseline was empty, and a baseline that lists
files keeps net-out's `empty_result` and its sentence (that check comes first).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from local_coder.agent import delegate
from local_coder.app_service import keep_changes, snapshot
from local_coder.general.coordinator import (
    GeneralError,
    list_requests,
    load_meta,
    project_id_for,
    run_general_request,
)
from local_coder.general.snapshot import apply_delta

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo_calc" / "repo"
GONE = "This result's working copy is gone, so there is nothing to keep. Your project was not changed."
NOTHING_LEFT = "This result has no files left, so Keep would empty your project. Your project was not changed."


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME with the data dir inside it, so the hosted agent may read and write as it does in the app."""
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(root / ".local" / "share" / "local-coder"))
    return root


def local(project: Path, request_id: str, engine) -> dict:
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd),
    )


def writes(path: str, body: str):
    def engine(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def state(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under root, with the bytes of each file and its mode: a refused Keep changes none."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None, p.stat().st_mode)
        for p in sorted(root.rglob("*"))
    }


def trees(tmp_path: Path, baseline: dict[str, str], candidate: dict[str, str] | None, user: dict[str, str]):
    """Three folders: B, C (None: the folder does not exist) and U."""
    folders = {}
    for name, files in (("baseline", baseline), ("candidate", candidate), ("user", user)):
        folder = tmp_path / name
        if files is not None:
            folder.mkdir()
            for rel, text in files.items():
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_text(text, encoding="utf-8")
        folders[name] = folder
    return folders["user"], folders["baseline"], folders["candidate"]


def refusal(action) -> tuple[str, str] | None:
    try:
        action()
    except GeneralError as exc:
        return exc.code, exc.detail
    return None


# --- apply_delta itself (also the MCP apply's gate) -------------------------------------------------------------


def test_apply_delta_refuses_a_gone_candidate_when_the_baseline_lists_no_files(tmp_path):
    user, baseline, candidate = trees(tmp_path, {}, None, {"mine.py": "M\n"})
    before = state(user)
    result = apply_delta(user, baseline, candidate)
    assert result == {"ok": False, "code": "result_gone", "detail": GONE, "applied": []}
    assert state(user) == before


def test_apply_delta_refuses_a_gone_candidate_before_it_looks_for_clashes(tmp_path):
    """He added a file meanwhile: the answer is still that the result is gone, and nothing is written."""
    user, baseline, candidate = trees(tmp_path, {}, None, {"a.py": "his own\n"})
    assert apply_delta(user, baseline, candidate)["code"] == "result_gone"
    assert (user / "a.py").read_text(encoding="utf-8") == "his own\n"


def test_apply_delta_still_applies_a_candidate_that_exists_and_is_empty_when_the_baseline_was_empty(tmp_path):
    user, baseline, candidate = trees(tmp_path, {}, {}, {"mine.py": "M\n"})
    assert apply_delta(user, baseline, candidate)["ok"] is True and (user / "mine.py").is_file()


def test_apply_delta_still_applies_a_new_file_onto_an_empty_baseline(tmp_path):
    user, baseline, candidate = trees(tmp_path, {}, {"new.py": "N\n"}, {})
    assert apply_delta(user, baseline, candidate)["applied"] == ["new.py"]


@pytest.mark.parametrize("candidate", [None, {}], ids=["folder missing", "folder empty"])
def test_apply_delta_keeps_net_outs_refusal_for_a_baseline_that_lists_files(tmp_path, candidate):
    files = {"a.py": "A\n", "src/b.py": "B\n"}
    user, baseline, folder = trees(tmp_path, files, candidate, files)
    result = apply_delta(user, baseline, folder)
    assert result == {"ok": False, "code": "empty_result", "detail": NOTHING_LEFT, "applied": []}


# --- Keep: a first request on an empty folder whose candidate/ is gone ---------------------------------------------


def test_keep_refuses_a_first_result_whose_candidate_is_gone_and_does_not_mark_it_kept(home):
    folder = home / "projects" / "board"
    folder.mkdir(parents=True)
    review = local(folder, "r1", writes("main.py", "print('hi')\n"))
    pid = project_id_for(folder)
    assert review["can_keep"] is True
    shutil.rmtree(Path(load_meta(pid, "r1")["candidate"]))
    assert refusal(lambda: keep_changes("r1", folder)) == ("result_gone", GONE)
    assert list(folder.rglob("*")) == [], "nothing was written"
    assert not load_meta(pid, "r1").get("kept")
    assert snapshot("r1", repo=folder)["outcome"] != "kept"
    assert [row["outcome"] for row in list_requests(folder)] != ["kept"]


def test_keep_of_a_first_result_whose_candidate_was_emptied_is_still_nothing_to_keep(home):
    folder = home / "projects" / "board"
    folder.mkdir(parents=True)
    run_general_request(folder, "request r1", request_id="r1", start_preview_after=False,
                        fake_engine=lambda cwd, _text, _cancel: writes("main.py", "print('hi')\n")(cwd))
    candidate = Path(load_meta(project_id_for(folder), "r1")["candidate"])
    shutil.rmtree(candidate)
    candidate.mkdir()
    assert refusal(lambda: keep_changes("r1", folder)) == (
        "nothing_to_keep", "This request changed nothing, so there is nothing to keep.",
    )
    assert list(folder.rglob("*")) == []


def test_keep_of_a_project_with_files_whose_candidate_is_gone_keeps_net_outs_sentence(home):
    project = home / "projects" / "calc"
    shutil.copytree(FIXTURE, project)
    local(project, "r1", writes("src/calc.py", "x = 1\n"))
    shutil.rmtree(Path(load_meta(project_id_for(project), "r1")["candidate"]))
    before = state(project)
    assert refusal(lambda: keep_changes("r1", project)) == ("empty_result", NOTHING_LEFT)
    assert state(project) == before


# --- The MCP apply ----------------------------------------------------------------------------------------------


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def test_the_mcp_apply_refuses_a_draft_of_a_fresh_repo_whose_work_folder_is_gone(home):
    """A project whose only marker is its `.git` folder has a baseline that lists no files (snapshots skip `.git`)."""
    app = home / "projects" / "fresh"
    (app / ".git").mkdir(parents=True)
    call = {"id": "1", "type": "function", "function": {
        "name": "write", "arguments": json.dumps({"path": "main.py", "content": "print('hi')\n"})}}
    steps = iter([_reply(calls=[call]), _reply("Wrote main.py.")])
    draft = delegate.create_draft(str(app), "Write main.py.", chat=lambda _m, _t: next(steps), home=home)
    assert draft["files"] == ["main.py"]
    shutil.rmtree(draft["work_folder"])
    before = state(app)
    result = delegate.apply_draft(draft["draft_id"])
    assert result == {"applied": False, "project": str(app), "code": "result_gone", "detail": GONE}
    assert state(app) == before
    assert delegate.discard_draft(draft["draft_id"]) == {"discarded": True, "draft_id": draft["draft_id"]}
