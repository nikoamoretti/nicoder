"""Agent tools: what each one returns, refuses, and records."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from local_coder.agent.access import AccessPolicy
from local_coder.agent.tools import ToolBox, expand_braces, glob_match, inside_data_root

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")


@pytest.fixture
def home(tmp_path) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    app = root / "projects" / "nicos-prototypes"
    (app / "app" / "prototypes" / "shadow-paris").mkdir(parents=True)
    (app / ".git").mkdir()
    (app / ".git" / "config").write_text('[remote "origin"]\n\turl = https://example.com/nico/prototypes.git\n')
    (app / "README.md").write_text("# Nico — Prototypes\n\nPages you can throw away.\n", encoding="utf-8")
    (app / "app" / "prototypes" / "shadow-paris" / "page.tsx").write_text(
        "export default function Page() {\n  return <h1>Shadow Paris</h1>;\n}\n", encoding="utf-8",
    )
    (app / "node_modules" / "dep").mkdir(parents=True)
    (app / "node_modules" / "dep" / "index.js").write_text("Shadow Paris inside a dependency\n")
    (app / ".env").write_text("TOKEN=shadow\n", encoding="utf-8")
    starter = root / "Local Coder Projects" / "check-paris-shadow"
    (starter / "src").mkdir(parents=True)
    (starter / "package.json").write_text('{"name": "check-paris-shadow"}', encoding="utf-8")
    (starter / "src" / "App.tsx").write_text("<h1>New app</h1><p>empty on purpose</p>", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_text("SSH-KEY-CONTENT", encoding="utf-8")
    (root / "notes.bin").write_bytes(b"\x00\x01binary")
    return root


def _ask_box(home: Path, **kwargs) -> ToolBox:
    return ToolBox(AccessPolicy(home=home), cwd=home, run_tmp=home.parent / "run-tmp", **kwargs)


def _build_box(home: Path, tmp_path: Path) -> tuple[ToolBox, Path, Path]:
    project = home / "projects" / "nicos-prototypes"
    work = home / ".local" / "share" / "lc" / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "App.tsx").write_text("const a = 1;\nconst b = 1;\n", encoding="utf-8")
    box = ToolBox(AccessPolicy(home=home, workdir=work, project=project), cwd=work, run_tmp=tmp_path / "run-tmp")
    return box, project, work


def test_brace_and_glob_matching():
    assert expand_braces("*.{ts,tsx}") == ["*.ts", "*.tsx"]
    assert glob_match("src/deep/App.tsx", "*.tsx")
    assert glob_match("src/deep/App.tsx", "src/**/*.TSX")
    assert glob_match("App.tsx", "**/*.tsx")
    assert glob_match("projects/Shadow-Paris", "*shadow*")
    assert not glob_match("src/App.css", "*.{ts,tsx}")


def test_inside_data_root_matches_case_differently_on_case_insensitive_volume(tmp_path):
    from local_coder.engines.opencode_config import data_root

    data_root().mkdir(parents=True, exist_ok=True)
    root = Path(os.path.realpath(data_root()))
    parts = root.parts
    if len(parts) < 2:
        pytest.skip("data root has no component to swap")
    swapped = Path(*parts[:-1]) / parts[-1].swapcase()
    if not swapped.exists():
        pytest.skip("volume is case-sensitive; swapped path does not exist")
    assert inside_data_root(swapped) is True


def test_tool_names_depend_on_the_mode(home, tmp_path):
    assert _ask_box(home, allow_open_project=True).names == [
        "find_project", "list", "glob", "grep", "read", "bash", "open_project",
    ]
    box, _, _ = _build_box(home, tmp_path)
    assert box.names == ["find_project", "list", "glob", "grep", "read", "write", "edit", "bash"]
    assert [spec["function"]["name"] for spec in box.schemas()] == box.names


def test_unknown_tool_and_bad_arguments_come_back_as_text(home):
    box = _ask_box(home)
    assert "no tool named 'write'" in box.call("write", {"path": "x", "content": "y"}).text
    bad = box.call("read", "{not json")
    assert not bad.ok and "not valid JSON" in bad.text
    missing = box.call("read", {})
    assert "needs the 'path' argument" in missing.text


def test_list_hides_private_entries(home):
    result = _ask_box(home).call("list", {"path": "~"})
    assert "projects/" in result.text
    assert ".ssh" not in result.text
    assert result.activity == "Looking in ~"


def test_glob_skips_noise_and_finds_folders(home):
    box = _ask_box(home)
    result = box.call("glob", {"pattern": "*shadow*", "path": "~/projects"})
    assert "nicos-prototypes/app/prototypes/shadow-paris/" in result.text
    assert "node_modules" not in result.text
    tsx = box.call("glob", {"pattern": "**/*.tsx", "path": "~/projects/nicos-prototypes"})
    assert "app/prototypes/shadow-paris/page.tsx" in tsx.text


def test_grep_finds_code_but_not_secrets_or_dependencies(home):
    result = _ask_box(home).call("grep", {"pattern": "(?i)shadow paris", "path": "~/projects"})
    assert "nicos-prototypes/app/prototypes/shadow-paris/page.tsx:2:" in result.text
    assert "node_modules" not in result.text
    assert ".env" not in result.text
    only_md = _ask_box(home).call("grep", {"pattern": "Prototypes", "path": "~/projects", "include": "*.md"})
    assert "README.md:1:" in only_md.text and "page.tsx" not in only_md.text


def test_read_numbers_lines_and_pages(home):
    box = _ask_box(home)
    path = "~/projects/nicos-prototypes/app/prototypes/shadow-paris/page.tsx"
    result = box.call("read", {"filePath": path, "offset": 2, "limit": 1})
    assert "     2\t  return <h1>Shadow Paris</h1>;" in result.text
    assert "Read on with offset=3" in result.text
    assert box.read_paths == ["~/projects/nicos-prototypes/app/prototypes/shadow-paris/page.tsx"]


@pytest.mark.parametrize("path,needle", [
    ("~/.ssh/id_rsa", "private"),
    ("~/projects/nicos-prototypes/.env", "secret"),
    ("~/notes.bin", "not a text file"),
    ("~/projects", "is a folder"),
])
def test_read_refusals(home, path, needle):
    result = _ask_box(home).call("read", {"path": path})
    assert not result.ok
    assert needle in result.text


def test_find_project_prefers_real_code_over_an_empty_starter(home):
    result = _ask_box(home).call("find_project", {"query": "the Paris Shadow app"})
    lines = [row for row in result.text.splitlines() if re.match(r"^\d+\. ", row)]
    assert lines[0].startswith("1. ~/projects/nicos-prototypes — Nico — Prototypes; git: example.com/nico/prototypes")
    assert "subfolder app/prototypes/shadow-paris" in lines[0]
    assert "empty Local Coder starter" in lines[1]


def test_open_project_needs_a_real_project(home):
    box = _ask_box(home, allow_open_project=True)
    assert "whole home folder" in box.call("open_project", {"path": "~"}).text
    (home / "plain").mkdir()
    assert "does not look like a project" in box.call("open_project", {"path": "~/plain"}).text
    opened = box.call("open_project", {"path": "~/projects/nicos-prototypes"})
    assert opened.ok and box.opened == home / "projects" / "nicos-prototypes"


def test_write_and_edit_change_only_the_working_copy(home, tmp_path):
    box, project, work = _build_box(home, tmp_path)
    created = box.call("write", {"path": str(project / "src" / "new.ts"), "content": "export {}\n"})
    assert created.ok and (work / "src" / "new.ts").is_file()
    assert not (project / "src" / "new.ts").exists()
    twice = box.call("edit", {"path": "src/App.tsx", "old_string": "= 1;", "new_string": "= 2;"})
    assert "appears 2 times" in twice.text
    edited = box.call("edit", {"path": "src/App.tsx", "oldString": "= 1;", "newString": "= 2;", "replaceAll": True})
    assert edited.ok and (work / "src" / "App.tsx").read_text() == "const a = 2;\nconst b = 2;\n"
    missing = box.call("edit", {"path": "src/App.tsx", "old_string": "nope", "new_string": "x"})
    assert "was not found" in missing.text
    outside = box.call("write", {"path": "~/projects/elsewhere.txt", "content": "x"})
    assert not outside.ok and "outside the project's working copy" in outside.text
    assert box.changed_paths == ["src/new.ts", "src/App.tsx"]


@darwin_only
def test_bash_in_a_change_request_writes_only_the_working_copy(home, tmp_path):
    box, project, work = _build_box(home, tmp_path)
    inside = box.call("bash", {"command": "echo built > out.txt && cat out.txt"})
    assert inside.ok and "built" in inside.text and "[exit code 0" in inside.text
    assert (work / "out.txt").read_text() == "built\n"
    outside = box.call("bash", {"command": f"echo x > '{project}/hacked.txt'"})
    assert not outside.ok and "Operation not permitted" in outside.text
    assert not (project / "hacked.txt").exists()
    secret = box.call("bash", {"command": f"cat '{home}/.ssh/id_rsa'"})
    assert not secret.ok and "Operation not permitted" in secret.text
    assert "SSH-KEY-CONTENT" not in secret.text


@darwin_only
def test_bash_in_a_question_cannot_write_project_files(home):
    box = ToolBox(
        AccessPolicy(home=home, project=home / "projects" / "nicos-prototypes"),
        cwd=home / "projects" / "nicos-prototypes",
        run_tmp=home.parent / "run-tmp",
    )
    listed = box.call("bash", {"command": "ls app/prototypes"})
    assert listed.ok and "shadow-paris" in listed.text
    blocked = box.call("bash", {"command": "touch README2.md"})
    assert not blocked.ok and not (home / "projects" / "nicos-prototypes" / "README2.md").exists()


@darwin_only
def test_bash_stops_when_the_user_presses_stop(home, tmp_path):
    cancel = threading.Event()
    box = _ask_box(home, cancel=cancel)
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    result = box.call("bash", {"command": "sleep 20"})
    assert time.monotonic() - started < 10
    assert "stopped by the user" in result.text
