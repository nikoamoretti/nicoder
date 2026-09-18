"""Mac-app integration surface: inspect, classify, explain, run, keep/discard."""
from __future__ import annotations

import io
import json
import threading

import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import (
    classify_legacy_request,
    classify_request,
    discard_changes,
    explain_request,
    inspect_legacy_project,
    inspect_project,
    keep_changes,
    run_request,
)
from local_coder.progress import latest_progress, phrase_for_event
from local_coder.runtime import episode_store

from .conftest import FIXTURE_REPO, make_controller


@pytest.fixture(autouse=True)
def isolated_app_config(tmp_path, monkeypatch):
    """Keep app prefs and project locks out of the developer's machine config."""
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(tmp_path / "machine-config"))


def test_inspect_python_fixture_is_ready(repo_copy):
    report = inspect_project(repo_copy)
    assert report["supported"] is True
    assert report["verdict"] == "ready"
    assert report["python_files"] >= 1
    assert report["test_files"] >= 1


def test_inspect_missing_folder(tmp_path):
    report = inspect_project(tmp_path / "nope")
    assert report["supported"] is False
    assert report["verdict"] == "missing"


def test_inspect_javascript_project_is_supported(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    for name in ("app.jsx", "main.tsx", "page.js", "view.js", "store.ts",
                 "hooks.ts", "widget.jsx", "index.js"):
        (root / name).write_text("export default 1\n", encoding="utf-8")
    report = inspect_project(root)
    assert report["supported"] is True
    assert report["verdict"] in {"ready", "empty"}


def test_inspect_dirty_git_is_allowed(repo_copy):
    import subprocess

    subprocess.run(["git", "init"], cwd=repo_copy, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_copy, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t.test", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=repo_copy, check=True, capture_output=True)
    (repo_copy / "src" / "calc.py").write_text("# dirty\n", encoding="utf-8")
    report = inspect_project(repo_copy)
    assert report["supported"] is True
    assert report["git"]["dirty"] is True


def test_classify_explain_and_web_are_allowed():
    assert classify_request("Explain how add works")["kind"] == "explain"
    kind = classify_request("add a search box", {"supported": True, "exists": True, "languages": ["Python"]})
    assert kind["kind"] == "code"
    assert classify_request("Fix add so it returns a sum",
                            {"supported": True, "exists": True, "languages": ["Python"]})["kind"] == "code"
    legacy = classify_legacy_request("add a search box", {"supported": True, "languages": ["Python"]})
    assert legacy["kind"] == "unsupported"
    assert inspect_legacy_project.__name__ == "inspect_legacy_project"


def test_explain_is_read_only_and_grounded(repo_copy):
    original = (repo_copy / "src" / "calc.py").read_bytes()
    result = explain_request(repo_copy, "Explain how add works")
    assert result["kind"] == "explain"
    assert result["hits"]
    assert any(row["path"].endswith("calc.py") for row in result["hits"])
    assert "return a - b" in result["hits"][0]["excerpt"] or "add" in result["summary"].lower()
    assert (repo_copy / "src" / "calc.py").read_bytes() == original


def test_progress_phrases_are_factual():
    assert phrase_for_event("patch_applied") == "Preparing a change."
    assert "Ready to review" in (phrase_for_event("handoff_written", {"outcome": "solved"}) or "")
    assert "Stopped" in (phrase_for_event("handoff_written", {"outcome": "blocked"}) or "")
    latest = latest_progress([
        {"seq": 0, "type": "episode_created", "data": {}},
        {"seq": 1, "type": "patch_applied", "data": {}},
    ])
    assert latest["phrase"] == "Preparing a change."


def test_app_run_keep_and_discard_via_service(repo_copy, state_dir, tmp_path):
    before = (repo_copy / "src" / "calc.py").read_bytes()
    result = run_request(
        repo_copy, "Fix add so it adds instead of subtracting.",
        profile_name="fake:success", execution="trusted-local",
        state_dir=state_dir, episode_id="ep-app-keep",
        ensure_local_server=False,
    )
    assert result["facts"]["outcome"] == "solved"
    assert result["can_keep"] is True
    assert (repo_copy / "src" / "calc.py").read_bytes() == before
    store = episode_store(state_dir)
    kept = keep_changes("ep-app-keep", repo_copy, store=store)
    assert kept["ok"] is True
    assert b"return a + b" in (repo_copy / "src" / "calc.py").read_bytes()

    other = tmp_path / "other"
    other.mkdir()
    import shutil

    shutil.copytree(FIXTURE_REPO, other, dirs_exist_ok=True)
    run_request(
        other, "Fix add so it adds instead of subtracting.",
        profile_name="fake:success", execution="trusted-local",
        state_dir=state_dir, episode_id="ep-app-discard",
        ensure_local_server=False,
    )
    original = (other / "src" / "calc.py").read_bytes()
    discarded = discard_changes("ep-app-discard", store=store)
    assert discarded["ok"] is True
    assert (other / "src" / "calc.py").read_bytes() == original
    assert not store.candidate_dir("ep-app-discard").exists()


def test_cancel_stops_before_model_steps(repo_copy, state_dir):
    from local_coder.inference.fake import demo_script

    cancel = threading.Event()
    cancel.set()
    controller, _ = make_controller(
        repo_copy, state_dir, demo_script("fake:success"), episode_id="ep-cancel")
    controller.cancel_event = cancel
    state = controller.run()
    assert state.outcome == "blocked"
    assert state.stop_reason == "cancelled by user"
    types = [event["type"] for event in controller.store.read_events("ep-cancel")]
    assert "cancelled" in types
    assert (repo_copy / "src" / "calc.py").read_text(encoding="utf-8").count("return a - b")


def test_rpc_inspect_and_classify(repo_copy):
    server = RpcServer()
    inspect = server.dispatch({
        "id": "1", "method": "inspect_project", "params": {"path": str(repo_copy)},
    })
    assert inspect["ok"] is True
    assert inspect["result"]["supported"] is True
    classified = server.dispatch({
        "id": "2", "method": "classify_request",
        "params": {"path": str(repo_copy), "text": "add a search box"},
    })
    assert classified["ok"] is True
    assert classified["result"]["kind"] == "code"
    ping = server.dispatch({"id": "3", "method": "ping", "params": {}})
    assert ping["result"]["ok"] is True


def test_rpc_serve_one_line(repo_copy):
    incoming = io.StringIO(json.dumps({
        "id": "9", "method": "inspect_project", "params": {"path": str(repo_copy)},
    }) + "\n")
    outgoing = io.StringIO()
    RpcServer(out=outgoing).serve(incoming)
    rows = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert rows[0]["ok"] is True
    assert rows[0]["result"]["name"] == repo_copy.name
