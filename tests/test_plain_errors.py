"""Plain words, not Python error text (cut core-fixes, problem 4).

No traceback, errno or exception name reaches the app. An unreadable folder is described, not raised;
unexpected errors in the helper and in a request's worker become one plain sentence while the raw text and
traceback go to the helper's stderr and to a file under <data dir>/logs/; the refusals the engine makes on
purpose (gone, discarded, kept, foreign server) keep their own sentences and codes.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest

from local_coder import errorlog
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, classify_request, inspect_project
from local_coder.engines.opencode_config import data_root
from local_coder.errorlog import UNEXPECTED, record_error
from local_coder.general.coordinator import GeneralError, run_general_request

from .conftest import TICKET_FILE

# What a person must never read in an error: Python's own words.
RAW = re.compile(r'Traceback|Errno|File "|\.py\b|\b[A-Za-z]*(?:Error|Exception)\b')


def _assert_plain(text: str) -> None:
    assert text.strip(), "an error needs words"
    assert not RAW.search(text), text
    assert text.count(".") <= 3, "one or two plain sentences"


def _rpc():
    out = io.StringIO()
    return RpcServer(runtime=AppRuntime(), out=out), out


def _log_text() -> str:
    path = data_root() / "logs" / errorlog.LOG_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def locked_folder(repo_copy):
    """A project folder nobody can read (chmod 000), restored afterwards so pytest can clean up."""
    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    repo_copy.chmod(0)
    try:
        yield repo_copy
    finally:
        repo_copy.chmod(0o755)


# --- Unreadable folders ------------------------------------------------------------------------------------


def test_an_unreadable_folder_is_described_not_raised(locked_folder):
    report = inspect_project(locked_folder)
    assert report["supported"] is False
    assert report["verdict"] == "unreadable"
    assert report["can_create"] is False
    assert "cannot read" in report["summary"]
    _assert_plain(report["summary"])
    verdict = classify_request(TICKET_FILE.read_text(encoding="utf-8"), report)
    assert verdict["kind"] == "unsupported"
    assert verdict["summary"] == report["summary"]


def test_an_unreadable_folder_through_the_helper_never_says_internal(locked_folder):
    server, out = _rpc()
    started = time.monotonic()
    inspected = server.dispatch({"id": "1", "method": "inspect_project", "params": {"path": str(locked_folder)}})
    classified = server.dispatch({
        "id": "2", "method": "classify_request",
        "params": {"path": str(locked_folder), "text": TICKET_FILE.read_text(encoding="utf-8")},
    })
    ran = server.dispatch({
        "id": "3", "method": "run_request",
        "params": {"path": str(locked_folder), "text": TICKET_FILE.read_text(encoding="utf-8")},
    })
    assert time.monotonic() - started < 2
    assert inspected["ok"] is True and inspected["result"]["supported"] is False
    assert classified["ok"] is True and classified["result"]["kind"] == "unsupported"
    assert ran["ok"] is False and ran["error"]["code"] == "unreadable"
    for text in (inspected["result"]["summary"], classified["result"]["summary"], ran["error"]["detail"]):
        _assert_plain(text)
    assert out.getvalue() == "", "no progress events"
    assert not (data_root() / "projects").exists(), "nothing was made in the data dir"


def test_a_folder_inside_a_locked_folder_is_described_too(repo_copy):
    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    inner = repo_copy / "src"
    repo_copy.chmod(0)
    try:
        report = inspect_project(inner)
    finally:
        repo_copy.chmod(0o755)
    assert report["supported"] is False
    _assert_plain(report["summary"])


# --- Unexpected errors in the helper ------------------------------------------------------------------------


def test_a_call_that_fails_unexpectedly_gets_one_plain_sentence(capfd):
    server, _out = _rpc()
    reply = server.dispatch({"id": "1", "method": "inspect_project", "params": {}})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "internal"
    assert reply["error"]["detail"] == UNEXPECTED
    _assert_plain(reply["error"]["detail"])
    assert "Traceback" not in json.dumps(reply)
    stderr = capfd.readouterr().err
    assert "Traceback" in stderr and "KeyError" in stderr, "the raw text goes to the helper's stderr"
    logged = _log_text()
    assert "Traceback" in logged and "inspect_project" in logged, "and to a file under logs/"


def test_a_missing_file_gets_a_plain_sentence(capfd):
    server, _out = _rpc()
    reply = server.dispatch({"id": "1", "method": "snapshot", "params": {"episode_id": "no-such-request"}})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "not_found"
    _assert_plain(reply["error"]["detail"])
    assert "no-such-request" not in reply["error"]["detail"]
    assert "no-such-request" in capfd.readouterr().err


def test_the_error_log_is_private_and_stays_small(tmp_path):
    record_error("test", ValueError("first"))
    path = data_root() / "logs" / errorlog.LOG_NAME
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("x" * (errorlog.MAX_LOG_BYTES + 1), encoding="utf-8")
    record_error("test", "second")
    assert "second" in path.read_text(encoding="utf-8")
    assert path.stat().st_size < errorlog.MAX_LOG_BYTES
    assert (path.parent / f"{errorlog.LOG_NAME}.1").is_file()


def test_the_error_log_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(blocker))
    record_error("test", ValueError("nowhere to write"))


# --- Failures in a request's worker -------------------------------------------------------------------------


def _run_worker(repo: Path) -> tuple[AppRuntime, list[dict]]:
    runtime = AppRuntime()
    events: list[dict] = []
    runtime.subscribe(events.append)
    runtime.start_request(str(repo), "Fix add so it adds instead of subtracting.")
    thread = runtime._thread
    if thread is not None:
        thread.join(10)
    return runtime, events


def test_a_worker_failure_is_one_plain_sentence_and_not_a_done(repo_copy, monkeypatch, capfd):
    def explode(*_args, **_kwargs):
        raise OSError(28, "No space left on device", str(repo_copy / "src" / "calc.py"))

    monkeypatch.setattr("local_coder.general.coordinator.run_general_request", explode)
    runtime, events = _run_worker(repo_copy)
    errors = [event for event in events if event["type"] == "app_error"]
    assert len(errors) == 1
    assert errors[0]["data"] == {"detail": UNEXPECTED, "code": "runtime"}
    assert errors[0]["detail"] == UNEXPECTED
    _assert_plain(errors[0]["detail"])
    assert all(event["type"] not in {"done", "stopped"} for event in events), "a failure is not a finished attempt"
    assert runtime.last_error == UNEXPECTED and runtime.is_running() is False
    assert "No space left on device" in capfd.readouterr().err
    assert "No space left on device" in _log_text()


def test_a_deliberate_refusal_in_the_worker_keeps_its_own_sentence(repo_copy, monkeypatch):
    sentence = "A model server is already running at http://127.0.0.1:8080 but Local Coder did not start it."

    def refuse(*_args, **_kwargs):
        raise GeneralError(sentence, code="foreign_server")

    monkeypatch.setattr("local_coder.general.coordinator.run_general_request", refuse)
    runtime, events = _run_worker(repo_copy)
    errors = [event for event in events if event["type"] == "app_error"]
    assert errors[0]["data"] == {"detail": sentence, "code": "foreign_server"}
    assert runtime.last_error == sentence


# --- Refusals the engine makes on purpose keep their sentences and codes -----------------------------------


@pytest.fixture
def ts_project(tmp_path):
    project = tmp_path / "web"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (project / "package.json").write_text('{"name":"web"}\n', encoding="utf-8")
    return project


def _waiting(project: Path, name: str) -> None:
    def engine(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "a.ts").write_text("export const a = 2\n", encoding="utf-8")

    run_general_request(project, "change a", request_id=name, fake_engine=engine, start_preview_after=False)


def _call(server: RpcServer, method: str, **params) -> dict:
    return server.dispatch({"id": "1", "method": method, "params": params})


def test_keep_after_a_hand_edit_says_so_instead_of_internal(ts_project):
    _waiting(ts_project, "req-conflict")
    (ts_project / "src" / "a.ts").write_text("HAND-EDIT\n", encoding="utf-8")
    server, _out = _rpc()
    reply = _call(server, "keep", episode_id="req-conflict", path=str(ts_project))
    assert reply["ok"] is False
    assert reply["error"]["code"] == "conflict"
    assert reply["error"]["detail"] == "Some files changed in the project while this request was running."
    assert (ts_project / "src" / "a.ts").read_text(encoding="utf-8") == "HAND-EDIT\n"


def test_keep_into_a_gone_folder_says_the_folder_is_gone(ts_project):
    _waiting(ts_project, "req-gone")
    shutil.rmtree(ts_project)
    server, _out = _rpc()
    reply = _call(server, "keep", episode_id="req-gone", path=str(ts_project))
    assert reply["ok"] is False
    assert reply["error"]["code"] == "folder_gone"
    assert "gone" in reply["error"]["detail"] and ts_project.name in reply["error"]["detail"]
    _assert_plain(reply["error"]["detail"])
    assert not ts_project.exists()


def test_keep_after_discard_is_refused_with_its_own_sentence(ts_project):
    _waiting(ts_project, "req-discarded")
    server, _out = _rpc()
    assert _call(server, "discard", episode_id="req-discarded", path=str(ts_project))["ok"] is True
    reply = _call(server, "keep", episode_id="req-discarded", path=str(ts_project))
    assert reply["ok"] is False and reply["error"]["code"] == "discarded"
    assert reply["error"]["detail"] == "This request was discarded, so there is nothing to keep."
    assert (ts_project / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 1\n"


def test_discard_after_keep_is_refused_with_its_own_sentence(ts_project):
    _waiting(ts_project, "req-kept")
    server, _out = _rpc()
    assert _call(server, "keep", episode_id="req-kept", path=str(ts_project))["ok"] is True
    again = _call(server, "keep", episode_id="req-kept", path=str(ts_project))
    discard = _call(server, "discard", episode_id="req-kept", path=str(ts_project))
    assert again["ok"] is False and again["error"]["code"] == "kept"
    assert discard["ok"] is False and discard["error"]["code"] == "kept"
    assert "original project was not changed" not in discard["error"]["detail"].lower()
    for reply in (again, discard):
        _assert_plain(reply["error"]["detail"])
    assert (ts_project / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 2\n"


def test_a_keep_that_cannot_write_says_so_without_the_system_text(ts_project, monkeypatch, capfd):
    _waiting(ts_project, "req-readonly")

    def refuse(*_args, **_kwargs):
        raise OSError(13, "Permission denied", str(ts_project / "src" / "a.ts"))

    monkeypatch.setattr("local_coder.general.snapshot._atomic_copy", refuse)
    server, _out = _rpc()
    reply = _call(server, "keep", episode_id="req-readonly", path=str(ts_project))
    assert reply["ok"] is False and reply["error"]["code"] == "apply_failed"
    _assert_plain(reply["error"]["detail"])
    assert "Permission denied" not in reply["error"]["detail"]
    assert "Permission denied" in capfd.readouterr().err
    assert (ts_project / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 1\n", "rolled back"
