"""Cut keep-honesty: Keep never overwrites or fakes a rename, and a refusal says what is true.

A file rename (the hosted agent's `bash` `mv` makes one; the local model cannot) is the one change whose two ends
Keep did not look at. It replaced a file he made at the destination, and it said "kept" for a destination it never
wrote when he had deleted or moved the source. Now Keep checks both ends like every other path it touches, before it
writes anything, and every refusal names the files and what happened to each. Nothing about the happy path changes.

Each test names the probe scenario it comes from (RN* renames, WD* the sentence, HM* the hosted agent's real `mv`).
Every refusal compares the folder before and after, file mode and links included: a refused Keep writes nothing.
"""
from __future__ import annotations

import io
import os
import shutil
import stat
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, discard_changes, keep_changes, snapshot
from local_coder.general.coordinator import GeneralError, list_requests, run_general_request

HEAD = "Not kept: since these changes were started, "
TAIL = "Your project was not changed."
MOVED = "export const moved = 'X'\n"
HIS = "export const nico = 'MINE'\n"
RED = "export const a = 'RED'\n"
BLUE = "export const b = 'BLUE'\n"


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
    root = home / "projects" / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (root / "src" / "old.ts").write_text(MOVED, encoding="utf-8")
    (root / "src" / "util.ts").write_text("export const util = 1\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"p"}\n', encoding="utf-8")
    (root / "README.md").write_text("# p\n", encoding="utf-8")
    return root


# --- The things the scenarios do and look at ---------------------------------------------------------------------


def local(project: Path, request_id: str, engine) -> dict:
    """A request whose engine edits the working copy (the local path's shape; Keep is one code path for all)."""
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd),
    )


def write(path: str, body: str):
    def engine(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def several(*engines):
    def engine(cwd: Path) -> None:
        for step in engines:
            step(cwd)

    return engine


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


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def refusal(project: Path, request_id: str) -> GeneralError:
    with pytest.raises(GeneralError) as raised:
        keep_changes(request_id, project)
    return raised.value


def refused_as_a_conflict(project: Path, request_id: str, what: str) -> None:
    """Keep is refused with code `conflict` and Nico's sentence for `what`, and the folder is exactly as it was."""
    before = state(project)
    error = refusal(project, request_id)
    assert error.code == "conflict"
    assert error.detail == f"{HEAD}{what}. {TAIL}"
    assert state(project) == before, "a refused Keep writes nothing"


def can_still_be_decided(project: Path, request_id: str) -> None:
    review = snapshot(request_id, repo=project)
    assert review["can_keep"] is True and review["can_discard"] is True and review["outcome"] == "review"


# --- Check 4: the refusal names the files and is true ------------------------------------------------------------


def test_wd1_one_file_changed_after_the_run(project):
    local(project, "r1", write("src/a.ts", RED))
    (project / "src" / "a.ts").write_text("export const a = 'NICO'\n", encoding="utf-8")
    before = state(project)
    error = refusal(project, "r1")
    assert error.code == "conflict"
    assert error.detail == (
        "Not kept: since these changes were started, src/a.ts was changed in your project. "
        "Your project was not changed."
    )
    assert state(project) == before
    can_still_be_decided(project, "r1")


def test_wd2_one_file_removed(project):
    local(project, "r1", write("src/util.ts", "export const util = 'CHANGED'\n"))
    (project / "src" / "util.ts").unlink()
    refused_as_a_conflict(project, "r1", "src/util.ts was removed from your project")


def test_wd3_one_file_added_at_a_path_the_request_adds(project):
    local(project, "r1", write("src/b.ts", BLUE))
    (project / "src" / "b.ts").write_text("export const b = 'NICO'\n", encoding="utf-8")
    refused_as_a_conflict(project, "r1", "src/b.ts was added to your project")


@pytest.mark.parametrize(
    ("count", "what"),
    [
        (2, "README.md and src/a.ts were changed in your project"),
        (3, "README.md, src/a.ts and src/old.ts were changed in your project"),
        (4, "README.md, src/a.ts, src/old.ts and 1 more file were changed in your project"),
    ],
)
def test_wd4_the_files_are_sorted_and_cut_at_three_names(project, count, what):
    names = ["README.md", "src/a.ts", "src/old.ts", "src/util.ts"][:count]
    local(project, "r1", several(*[write(name, "REQUEST\n") for name in names]))
    for name in names:
        (project / name).write_text("NICO\n", encoding="utf-8")
    refused_as_a_conflict(project, "r1", what)


def test_wd5_an_edit_made_while_the_first_request_waited_is_not_blamed_on_a_running_request(project):
    """The old sentence put the change down to a request that was still running: untrue here, he edited a.ts when R1
    had already finished, before R2 began. The new one only says what happened to the file."""
    local(project, "r1", write("src/a.ts", RED))
    (project / "src" / "a.ts").write_text("export const a = 'NICO'\n", encoding="utf-8")
    local(project, "r2", write("src/b.ts", BLUE))
    refused_as_a_conflict(project, "r2", "src/a.ts was changed in your project")
    assert discard_changes("r2", repo=project)["discarded"] is True
    refused_as_a_conflict(project, "r1", "src/a.ts was changed in your project")


def test_wd6_each_file_is_named_with_its_own_verb(project):
    local(project, "r1", several(write("src/a.ts", RED), write("src/util.ts", "export const util = 'RED'\n")))
    (project / "src" / "a.ts").write_text("export const a = 'NICO'\n", encoding="utf-8")
    (project / "src" / "util.ts").unlink()
    refused_as_a_conflict(
        project, "r1", "src/a.ts was changed in your project and src/util.ts was removed from your project",
    )


# --- Check 5: the other refusals, and the helper's error object, are unchanged ---------------------------------


def test_wd7_the_other_refusals_keep_their_own_sentences_and_codes(project, home):
    local(project, "r1", write("src/a.ts", RED))
    assert keep_changes("r1", project)["ok"] is True
    again = refusal(project, "r1")
    assert (again.code, again.detail) == ("kept", "These changes are already in your project.")

    other = home / "projects" / "other"
    shutil.copytree(project, other)
    (other / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    local(other, "r1", write("src/a.ts", RED))
    assert discard_changes("r1", repo=other)["discarded"] is True
    discarded = refusal(other, "r1")
    assert discarded.code == "discarded"
    assert discarded.detail == "This request was discarded, so there is nothing to keep."

    gone = home / "projects" / "gone"
    shutil.copytree(project, gone)
    local(gone, "r1", write("src/b.ts", BLUE))
    shutil.rmtree(gone)
    missing = refusal(gone, "r1")
    assert missing.code == "folder_gone" and "is gone, so there is nowhere to keep these changes" in missing.detail
    for error in (again, discarded, missing):
        assert not error.detail.startswith("Not kept"), "only a clash reads like this"
        assert "your project was not changed" not in error.detail.lower()


def test_the_helper_error_object_for_a_clash_has_only_a_code_and_a_detail(project):
    local(project, "r1", write("src/a.ts", RED))
    (project / "src" / "a.ts").write_text("export const a = 'NICO'\n", encoding="utf-8")
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    reply = server.dispatch({"id": "1", "method": "keep", "params": {"episode_id": "r1", "path": str(project)}})
    assert reply["ok"] is False
    assert reply["error"] == {
        "code": "conflict",
        "detail": f"{HEAD}src/a.ts was changed in your project. {TAIL}",
    }
