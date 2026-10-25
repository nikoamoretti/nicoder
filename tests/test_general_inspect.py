"""General inspect/classify no longer refuses JS, empty folders, or dirty trees."""
from __future__ import annotations

import subprocess

from local_coder.app_service import classify_legacy_request, classify_request, inspect_legacy_project, inspect_project


def test_inspect_javascript_project_is_ready(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text('{"name":"web","scripts":{"dev":"vite"}}\n', encoding="utf-8")
    for name in ("app.jsx", "main.tsx", "page.js", "view.js", "store.ts",
                 "hooks.ts", "widget.jsx", "index.js"):
        (root / name).write_text("export default 1\n", encoding="utf-8")
    report = inspect_project(root)
    assert report["supported"] is True
    assert report["verdict"] == "ready"
    assert "JavaScript" in report["summary"]


def test_inspect_empty_folder_is_usable(tmp_path):
    root = tmp_path / "blank"
    root.mkdir()
    report = inspect_project(root)
    assert report["supported"] is True
    assert report["verdict"] == "empty"


def test_inspect_dirty_git_is_not_blocking(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("export {}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t.test", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    (root / "src" / "app.ts").write_text("export const dirty = 1\n", encoding="utf-8")
    report = inspect_project(root)
    assert report["supported"] is True
    assert report["git"]["dirty"] is True
    assert any("uncommitted" in note.lower() for note in report["notes"])


def test_classify_web_and_search_box_are_code():
    kind = classify_request("add a search box", {"supported": True, "exists": True, "languages": ["Python"]})
    assert kind["kind"] == "code"
    assert classify_request("Build a polished task-board web app")["kind"] == "code"
    assert classify_request("Can you make this a standalone mac app?")["kind"] == "code"
    assert classify_request("is this a React project?")["kind"] == "chat"


def test_legacy_inspect_and_classify_still_refuse_javascript(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    for name in ("app.jsx", "main.tsx", "page.js", "view.js", "store.ts",
                 "hooks.ts", "widget.jsx", "index.js"):
        (root / name).write_text("export default 1\n", encoding="utf-8")
    report = inspect_legacy_project(root)
    assert report["verdict"] == "unsupported"
    kind = classify_legacy_request("add a search box", {"supported": True, "languages": ["Python"]})
    assert kind["kind"] == "unsupported"


def test_do_this_is_work_and_do_you_is_a_question():
    # Nico's spec (2026-10-25) began "do this", and "do" was read as the start of a question.
    spec = "do this * Data Ingestion: Scrape and ingest the Interstat datasets, which provide crime figures."
    assert classify_request(spec)["kind"] == "code"
    assert classify_request("scrape the Interstat datasets")["kind"] == "code"
    assert classify_request("set up a postgres database")["kind"] == "code"
    assert classify_request("do you know how the scraper works")["kind"] == "chat"
    assert classify_request("is this a React project?")["kind"] == "chat"
