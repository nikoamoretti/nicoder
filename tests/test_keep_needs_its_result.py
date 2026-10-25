"""Keep never empties a project (cut net-out, the folded-in wipe fix).

When a waiting request's `candidate/` folder was gone, emptied or unreadable, or the request had deleted every file,
the card listed every project file as changed with Keep on, and Keep deleted them all: the folder ended empty.
`apply_delta` now refuses, before it writes anything, when the baseline lists files and the candidate lists none. It
sits in `apply_delta` so that Keep and the MCP apply are protected alike and a refusal stays transactional. The card
of such a result offers Discard but not Keep and says why; it still lists the files and the diff (else the app would
tell him the request changed nothing).

Known limit, a later cut (`frozen-candidate-index`): a candidate that is missing one file, or has one unreadable
subfolder, still lists the rest and so still deletes those files under a normal card.

Every refusal compares the folder before and after, file modes included: a refused Keep writes nothing.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from local_coder.agent import delegate
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, discard_changes, keep_changes, snapshot
from local_coder.general.coordinator import (
    GeneralError,
    list_requests,
    project_id_for,
    request_dir,
    run_general_request,
)
from local_coder.general.snapshot import apply_delta, index_tree
from local_coder.general.starter import starter_root

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo_calc" / "repo"
NOTHING_LEFT = "This result has no files left, so Keep would empty your project. Your project was not changed."
CODE = "empty_result"
PROJECT_FILES = [
    "conftest.py", "local-coder.toml", "src/__init__.py", "src/calc.py", "tests/__init__.py", "tests/test_calc.py",
]

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="the agent's bash runs in sandbox-exec: macOS only")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME with the data dir inside it, so the hosted agent may read and write as it does in the app."""
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(root / ".local" / "share" / "local-coder"))
    return root


@pytest.fixture
def project(home) -> Path:
    """A copy of the demo calculator: six files, three tests."""
    root = home / "projects" / "calc"
    shutil.copytree(FIXTURE, root)
    return root


# --- The things the scenarios do and look at ------------------------------------------------------------------


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


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def deletes_everything(cwd: Path) -> None:
    for path in sorted(cwd.rglob("*")):
        if path.is_file():
            path.unlink()


def state(root: Path) -> dict[str, tuple]:
    """Everything under root: bytes and mode of each file, the target of each link, each folder."""
    found: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            found[rel] = ("folder",)
        else:
            found[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return found


def candidate_of(project: Path, request_id: str) -> Path:
    return request_dir(project_id_for(project), request_id) / "candidate"


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def refusal(action) -> tuple[str, str]:
    with pytest.raises(GeneralError) as raised:
        action()
    return raised.value.code, raised.value.detail


def empty_the_folder(folder: Path) -> None:
    for child in list(folder.iterdir()):
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def assert_card_offers_discard_only(project: Path, request_id: str) -> dict:
    """The result has nothing left: Keep is off and says why, Discard is on, and the files and the diff still show."""
    review = snapshot(request_id, repo=project)
    assert review["can_keep"] is False and review["can_discard"] is True
    assert review["summary"] == NOTHING_LEFT
    assert review["facts"]["files"] == PROJECT_FILES, "the files stay listed, else the app says nothing changed"
    assert review["diff"] != "", "and so does the diff"
    assert review["outcome"] == "review" and review["blocked_by"] is None
    return review


def assert_keep_is_refused_and_discard_goes_through(project: Path, request_id: str) -> None:
    before = state(project)
    assert refusal(lambda: keep_changes(request_id, project)) == (CODE, NOTHING_LEFT)
    assert state(project) == before, "a refused Keep writes nothing, not even a mode"
    assert not any(row[1] == "kept" for row in history(project)), "and closes nothing"
    assert discard_changes(request_id, repo=project)["discarded"] is True
    assert state(project) == before, "Discard never touches the project"


# --- The wipe, from every way a result can lose its files ---------------------------------------------------------


def test_a_candidate_folder_that_is_gone_is_not_kept_and_nothing_is_deleted(project):
    local(project, "r1", writes("src/gen.py", "GEN = 1\n"))
    shutil.rmtree(candidate_of(project, "r1"))
    assert_card_offers_discard_only(project, "r1")
    assert_keep_is_refused_and_discard_goes_through(project, "r1")


def test_an_emptied_candidate_folder_is_not_kept_and_nothing_is_deleted(project):
    local(project, "r1", writes("src/gen.py", "GEN = 1\n"))
    empty_the_folder(candidate_of(project, "r1"))
    assert candidate_of(project, "r1").is_dir()
    assert_card_offers_discard_only(project, "r1")
    assert_keep_is_refused_and_discard_goes_through(project, "r1")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder whatever its mode")
def test_a_candidate_folder_nobody_can_read_is_not_kept_and_nothing_is_deleted(project):
    local(project, "r1", writes("src/gen.py", "GEN = 1\n"))
    folder = candidate_of(project, "r1")
    folder.chmod(0)
    try:
        assert_card_offers_discard_only(project, "r1")
        assert_keep_is_refused_and_discard_goes_through(project, "r1")
    finally:
        folder.chmod(0o755)


def test_a_request_that_deleted_every_file_is_not_kept_and_nothing_is_deleted(project):
    review = local(project, "r1", deletes_everything)
    assert review["facts"]["files"] == PROJECT_FILES, "the card lists what it deleted"
    assert_card_offers_discard_only(project, "r1")
    assert_keep_is_refused_and_discard_goes_through(project, "r1")


@darwin_only
def test_a_hosted_agent_that_removed_every_file_with_bash_is_not_kept_and_nothing_is_deleted(project):
    """The agent's real `bash` (the only way it deletes a file) removes everything in the working copy."""
    steps = iter([
        _reply(calls=[_call("bash", {"command": "rm -rf conftest.py local-coder.toml src tests"}, "rm")]),
        _reply("Removed everything."),
    ])
    review = run_general_request(project, "Remove everything", request_id="r1",
                                 agent_chat=lambda _messages, _tools: next(steps), start_preview_after=False)
    assert review["facts"]["delta"]["deleted"] == PROJECT_FILES, "the real tool deleted them in the working copy"
    assert_card_offers_discard_only(project, "r1")
    assert_keep_is_refused_and_discard_goes_through(project, "r1")


def test_an_older_result_whose_candidate_is_gone_cannot_wipe_the_project_after_the_newer_one_is_kept(project):
    """R2 is built on R1; R1's folder is lost; Keep R2 goes through, and R1 (no longer held back) is refused."""
    local(project, "r1", writes("src/gen.py", "GEN = 1\n"))
    local(project, "r2", writes("src/other.py", "OTHER = 1\n"))
    shutil.rmtree(candidate_of(project, "r1"))
    kept = keep_changes("r2", project)
    assert kept["ok"] is True and kept["applied"] == ["src/gen.py", "src/other.py"]
    before = state(project)
    assert (project / "src" / "calc.py").is_file() and (project / "src" / "other.py").is_file()
    older = snapshot("r1", repo=project)
    assert older["can_keep"] is False and older["can_discard"] is True and older["summary"] == NOTHING_LEFT
    assert refusal(lambda: keep_changes("r1", project)) == (CODE, NOTHING_LEFT)
    assert state(project) == before
    assert discard_changes("r1", repo=project)["discarded"] is True
    assert state(project) == before


def test_the_refusal_reaches_the_app_as_one_plain_sentence_with_its_own_code(project):
    local(project, "r1", writes("src/gen.py", "GEN = 1\n"))
    shutil.rmtree(candidate_of(project, "r1"))
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    reply = server.dispatch({"id": "1", "method": "keep", "params": {"episode_id": "r1", "path": str(project)}})
    assert reply["ok"] is False and reply["error"] == {"code": CODE, "detail": NOTHING_LEFT}


# --- What the guard must not touch ----------------------------------------------------------------------------


def test_a_result_that_deletes_some_files_but_not_all_is_kept_as_before(project):
    def deletes_the_tests(cwd: Path) -> None:
        shutil.rmtree(cwd / "tests")

    review = local(project, "r1", deletes_the_tests)
    assert review["can_keep"] is True and review["can_discard"] is True
    kept = keep_changes("r1", project)
    assert kept["ok"] is True and kept["applied"] == ["tests/__init__.py", "tests/test_calc.py"]
    assert not (project / "tests" / "test_calc.py").exists() and (project / "src" / "calc.py").is_file()


def test_a_new_project_with_nothing_in_it_is_kept_as_before(home):
    empty = home / "projects" / "fresh"
    empty.mkdir(parents=True)
    review = local(empty, "r1", writes("main.py", "print('hi')\n"))
    assert review["can_keep"] is True
    # The blank starter is laid in the request's working copy (an empty folder gets it), so Keep writes it too.
    want = sorted([*index_tree(starter_root()).files, "main.py"])
    assert keep_changes("r1", empty)["applied"] == want
    assert sorted(p.relative_to(empty).as_posix() for p in empty.rglob("*") if p.is_file()) == want
    assert (empty / "main.py").read_text(encoding="utf-8") == "print('hi')\n"


# --- apply_delta itself (also the MCP apply's gate) -------------------------------------------------------------


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


@pytest.mark.parametrize("candidate", [None, {}], ids=["folder missing", "folder empty"])
def test_apply_delta_refuses_when_the_baseline_lists_files_and_the_candidate_none(tmp_path, candidate):
    files = {"a.py": "A\n", "src/b.py": "B\n"}
    user, baseline, folder = trees(tmp_path, files, candidate, files)
    result = apply_delta(user, baseline, folder)
    assert result == {"ok": False, "code": CODE, "detail": NOTHING_LEFT, "applied": []}
    assert (user / "a.py").read_text(encoding="utf-8") == "A\n" and (user / "src" / "b.py").is_file()


def test_apply_delta_refuses_before_it_looks_for_clashes(tmp_path):
    """He edited a file meanwhile: the answer is still that nothing is left, not a clash that blames his edit."""
    user, baseline, candidate = trees(tmp_path, {"a.py": "A\n"}, {}, {"a.py": "A, edited by hand\n"})
    assert apply_delta(user, baseline, candidate)["code"] == CODE
    assert (user / "a.py").read_text(encoding="utf-8") == "A, edited by hand\n"


def test_apply_delta_still_applies_when_one_file_is_left_or_the_baseline_was_empty(tmp_path):
    both = {"a.py": "A\n", "b.py": "B\n"}
    user, baseline, candidate = trees(tmp_path, both, {"a.py": "A\n"}, both)
    assert apply_delta(user, baseline, candidate)["ok"] is True and not (user / "b.py").exists()
    again = tmp_path / "again"
    again.mkdir()
    user, baseline, candidate = trees(again, {}, {"new.py": "N\n"}, {})
    assert apply_delta(user, baseline, candidate)["applied"] == ["new.py"]
    none = tmp_path / "none"
    none.mkdir()
    user, baseline, candidate = trees(none, {}, {}, {"mine.py": "M\n"})
    assert apply_delta(user, baseline, candidate)["ok"] is True and (user / "mine.py").is_file()


# --- The MCP apply ----------------------------------------------------------------------------------------------


def test_the_mcp_apply_refuses_a_draft_whose_work_folder_is_gone(home):
    app = home / "projects" / "app"
    (app / "src").mkdir(parents=True)
    (app / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    (app / "src" / "price.ts").write_text("export const price = 10;\n", encoding="utf-8")
    steps = iter([
        _reply(calls=[_call("edit", {"path": "src/price.ts", "old_string": "10", "new_string": "12"}, "1")]),
        _reply("Changed the price to 12."),
    ])
    draft = delegate.create_draft(str(app), "Set the price to 12.", chat=lambda _m, _t: next(steps), home=home)
    shutil.rmtree(draft["work_folder"])
    before = state(app)
    result = delegate.apply_draft(draft["draft_id"])
    assert result == {"applied": False, "project": str(app), "code": CODE, "detail": NOTHING_LEFT}
    assert state(app) == before, "the project was not emptied"
    assert delegate.discard_draft(draft["draft_id"]) == {"discarded": True, "draft_id": draft["draft_id"]}
