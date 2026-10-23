"""A request never touches a folder that is gone, and Keep never recreates one (cut core-fixes).

Problem 1: a request on a project folder that no longer exists (or is a file) is refused up front in
plain words, before anything is created and before any model call. Fix A: Keep refuses when the
folder is gone. Folders are created only by New Project and the welcome chat.
"""
from __future__ import annotations

import io
import shutil
import time
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, classify_request, inspect_project
from local_coder.engines.opencode_config import data_root
from local_coder.general.coordinator import (
    GeneralError,
    keep_request,
    list_requests,
    prepare_workspace,
    review_payload,
    run_general_request,
)

from .conftest import TICKET_FILE


def _request_folders() -> list[Path]:
    """Every request folder the engine has made in the (test) data dir."""
    root = data_root() / "projects"
    return sorted(root.glob("*/requests/*")) if root.exists() else []


def _never(*_args, **_kwargs):
    raise AssertionError("the engine must not start for a folder that is gone")


@pytest.fixture
def gone_folder(repo_copy):
    """A project that was open, then deleted."""
    assert inspect_project(repo_copy)["supported"] is True
    shutil.rmtree(repo_copy)
    return repo_copy


@pytest.fixture
def file_path(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not a project\n", encoding="utf-8")
    return path


def _rpc():
    out = io.StringIO()
    return RpcServer(runtime=AppRuntime(), out=out), out


def test_inspecting_a_gone_folder_offers_to_create_nothing(gone_folder):
    report = inspect_project(gone_folder)
    assert report["exists"] is False
    assert report["supported"] is False
    assert report["verdict"] == "missing"
    assert report["can_create"] is False
    assert gone_folder.name in report["summary"]
    assert "create a new project here" not in report["summary"].lower()
    assert report["issues"][0]["blocking"] is True
    assert not gone_folder.exists(), "inspecting must not make the folder"


def test_inspecting_a_file_says_it_is_a_file(file_path):
    report = inspect_project(file_path)
    assert report["exists"] is False
    assert report["supported"] is False
    assert report["verdict"] == "not_a_folder"
    assert report["can_create"] is False
    assert "file" in report["summary"].lower() and "not a folder" in report["summary"].lower()


def test_a_gone_folder_is_unsupported_for_any_kind_of_message(gone_folder):
    inspection = inspect_project(gone_folder)
    ticket = TICKET_FILE.read_text(encoding="utf-8")
    for text in (ticket, "What does add do?", "Explain how add works"):
        verdict = classify_request(text, inspection)
        assert verdict["kind"] == "unsupported", text
        assert gone_folder.name in verdict["summary"]
        assert verdict["summary"].count(".") <= 2, "one or two plain sentences"


def test_a_file_is_unsupported_and_says_so(file_path):
    verdict = classify_request("Fix add", inspect_project(file_path))
    assert verdict["kind"] == "unsupported"
    assert "is a file, not a folder" in verdict["summary"]


def test_an_existing_empty_folder_still_accepts_a_request(tmp_path):
    empty = tmp_path / "blank"
    empty.mkdir()
    verdict = classify_request("Build a task board", inspect_project(empty))
    assert verdict["kind"] == "code"
    meta = prepare_workspace(empty, "req-blank", allow_starter=True)
    assert meta["starter_used"] is True


def test_run_request_on_a_gone_folder_is_refused_before_a_worker_starts(gone_folder):
    server, out = _rpc()
    started = time.monotonic()
    reply = server.dispatch({
        "id": "1", "method": "run_request",
        "params": {"path": str(gone_folder), "text": TICKET_FILE.read_text(encoding="utf-8")},
    })
    assert time.monotonic() - started < 1.0
    assert reply["ok"] is False
    assert reply["error"]["code"] == "missing"
    assert gone_folder.name in reply["error"]["detail"]
    status = server.dispatch({"id": "2", "method": "run_status", "params": {}})["result"]
    assert status["running"] is False and status["episode_id"] is None
    assert out.getvalue() == "", "no progress events"
    assert not gone_folder.exists(), "the folder must stay gone"
    assert _request_folders() == []
    assert not (data_root() / "projects").exists(), "nothing was made in the data dir"


def test_run_request_on_a_file_leaves_the_file_alone(file_path):
    server, out = _rpc()
    reply = server.dispatch({"id": "1", "method": "run_request", "params": {"path": str(file_path), "text": "Fix it"}})
    assert reply["ok"] is False and reply["error"]["code"] == "not_a_folder"
    assert "file, not a folder" in reply["error"]["detail"]
    assert file_path.read_text(encoding="utf-8") == "not a project\n"
    assert out.getvalue() == ""
    assert _request_folders() == []


def test_the_engine_itself_refuses_a_gone_folder(gone_folder):
    """No other caller of run_general_request can bring the folder back either."""
    with pytest.raises(GeneralError) as raised:
        run_general_request(
            gone_folder, "Fix add", request_id="req-gone", fake_engine=_never, start_preview_after=False,
        )
    assert raised.value.code == "missing"
    assert gone_folder.name in raised.value.detail
    assert not gone_folder.exists()
    assert _request_folders() == []


def test_the_engine_itself_refuses_a_file(file_path):
    with pytest.raises(GeneralError) as raised:
        run_general_request(file_path, "Fix it", request_id="req-file", fake_engine=_never, start_preview_after=False)
    assert raised.value.code == "not_a_folder"
    assert file_path.read_text(encoding="utf-8") == "not a project\n"


def test_preparing_a_workspace_never_makes_the_project_folder(gone_folder):
    with pytest.raises(GeneralError) as raised:
        prepare_workspace(gone_folder, "req-prep", allow_starter=True)
    assert raised.value.code == "missing"
    assert not gone_folder.exists()
    assert _request_folders() == []


def test_looking_things_up_for_a_gone_folder_creates_nothing(gone_folder):
    from local_coder.app_service import snapshot

    assert list_requests(gone_folder) == []
    with pytest.raises(FileNotFoundError):
        snapshot("req-nope", repo=gone_folder)
    assert _request_folders() == []
    assert not (data_root() / "projects").exists()


# --- Fix A: Keep into a folder that is gone ------------------------------------------------------------------


@pytest.fixture
def ts_project(tmp_path):
    project = tmp_path / "web"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (project / "package.json").write_text('{"name":"web"}\n', encoding="utf-8")
    return project


def _waiting_result(project: Path, name: str, *, add: bool) -> dict:
    def engine(candidate: Path, _text: str, _cancel) -> None:
        if add:
            (candidate / "src" / "b.ts").write_text("export const b = 2\n", encoding="utf-8")
        else:
            (candidate / "src" / "a.ts").write_text("export const a = 'changed'\n", encoding="utf-8")

    return run_general_request(project, "change it", request_id=name, fake_engine=engine, start_preview_after=False)


@pytest.mark.parametrize("add", [True, False], ids=["adds-a-file", "modifies-a-file"])
def test_keep_refuses_when_the_folder_is_gone(ts_project, add):
    review = _waiting_result(ts_project, "req-keep-gone", add=add)
    assert review["can_keep"] is True
    shutil.rmtree(ts_project)
    with pytest.raises(GeneralError) as raised:
        keep_request(review["project_id"], "req-keep-gone", ts_project)
    assert raised.value.code == "folder_gone"
    assert "gone" in raised.value.detail and ts_project.name in raised.value.detail
    assert "Some files changed" not in raised.value.detail
    assert not ts_project.exists(), "Keep must not recreate the folder"
    # The waiting result stays on disk and can still be read.
    candidate = data_root() / "projects" / review["project_id"] / "requests" / "req-keep-gone" / "candidate"
    assert candidate.is_dir()
    after = review_payload(review["project_id"], "req-keep-gone")
    assert after["can_keep"] is True and after["facts"]["files"]


def test_keep_refuses_when_the_folder_was_replaced_by_a_file(ts_project):
    review = _waiting_result(ts_project, "req-keep-file", add=True)
    shutil.rmtree(ts_project)
    ts_project.write_text("now a file\n", encoding="utf-8")
    with pytest.raises(GeneralError) as raised:
        keep_request(review["project_id"], "req-keep-file", ts_project)
    assert raised.value.code == "not_a_folder"
    assert "is a file, not a folder" in raised.value.detail
    assert ts_project.read_text(encoding="utf-8") == "now a file\n"


def test_keep_still_works_when_the_folder_is_there(ts_project):
    review = _waiting_result(ts_project, "req-keep-ok", add=True)
    kept = keep_request(review["project_id"], "req-keep-ok", ts_project)
    assert kept["ok"] is True and kept["applied"] == ["src/b.ts"]
    assert (ts_project / "src" / "b.ts").read_text(encoding="utf-8") == "export const b = 2\n"
