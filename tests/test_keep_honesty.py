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
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, discard_changes, keep_changes, snapshot
from local_coder.general.coordinator import GeneralError, list_requests, run_general_request

HEAD = "Not kept: since these changes were started, "
TAIL = "Your project was not changed."
GENERIC = (
    "Local Coder could not write these changes into your project. "
    "Check that its folder and files can be changed, then try Keep again."
)
WENT_IN = "These changes went in with a later request you kept."
MOVED = "export const moved = 'X'\n"
HIS = "export const nico = 'MINE'\n"
HIS_README = "# p\nNico's own paragraph\n"
RED = "export const a = 'RED'\n"
BLUE = "export const b = 'BLUE'\n"
LONG_AGO = 1_000_000_000_000_000_000  # ns: any rewrite of a file moves its mtime off this

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


def move(source: str, destination: str):
    def engine(cwd: Path) -> None:
        (cwd / destination).parent.mkdir(parents=True, exist_ok=True)
        (cwd / source).rename(cwd / destination)

    return engine


def write(path: str, body: str):
    def engine(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def remove(path: str):
    def engine(cwd: Path) -> None:
        (cwd / path).unlink()

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


def is_moved(project: Path) -> bool:
    """src/new.ts holds the moved text and src/old.ts is gone."""
    new, old = project / "src" / "new.ts", project / "src" / "old.ts"
    return new.is_file() and new.read_text(encoding="utf-8") == MOVED and not old.exists()


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


# --- Check 1: a file he made at a rename's destination is never overwritten ---------------------------------------


def test_rn1_a_file_he_made_at_the_destination_is_not_replaced(project):
    """The request moves src/old.ts to src/new.ts; he writes his own src/new.ts while the result waits."""
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "new.ts").write_text(HIS, encoding="utf-8")
    refused_as_a_conflict(project, "r1", "src/new.ts was added to your project")
    assert (project / "src" / "new.ts").read_text(encoding="utf-8") == HIS
    assert (project / "src" / "old.ts").read_text(encoding="utf-8") == MOVED
    can_still_be_decided(project, "r1")
    assert history(project) == [("r1", "review")]


def test_rn7_in_a_stack_the_clash_is_refused_and_the_newer_request_can_still_be_discarded(project):
    """R1 moves; he writes src/new.ts; R2 adds src/b.ts. Keep R2 is refused. Discard R2: Keep R1 alone is refused the
    same way, because the clash is R1's own."""
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "new.ts").write_text(HIS, encoding="utf-8")
    local(project, "r2", write("src/b.ts", BLUE))
    refused_as_a_conflict(project, "r2", "src/new.ts was added to your project")
    can_still_be_decided(project, "r2")
    assert discard_changes("r2", repo=project)["discarded"] is True
    before = state(project)
    refused_as_a_conflict(project, "r1", "src/new.ts was added to your project")
    assert state(project) == before and not (project / "src" / "b.ts").exists(), "src/b.ts never lands"
    assert (project / "src" / "new.ts").read_text(encoding="utf-8") == HIS
    assert history(project) == [("r2", "discarded"), ("r1", "review")]


def test_rn9_when_the_move_is_the_second_requests_own_the_clash_is_refused_too(project):
    local(project, "r1", write("src/a.ts", RED))
    local(project, "r2", move("src/old.ts", "src/new.ts"))
    (project / "src" / "new.ts").write_text(HIS, encoding="utf-8")
    refused_as_a_conflict(project, "r2", "src/new.ts was added to your project")
    assert (project / "src" / "new.ts").read_text(encoding="utf-8") == HIS
    assert history(project) == [("r2", "review"), ("r1", "review")]


def test_rn12_a_link_he_made_at_the_destination_is_not_replaced(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "new.ts").symlink_to("../README.md")
    refused_as_a_conflict(project, "r1", "src/new.ts was added to your project")
    assert os.readlink(project / "src" / "new.ts") == "../README.md" and (project / "src" / "old.ts").is_file()
    can_still_be_decided(project, "r1")


def test_rn12b_a_link_he_put_where_the_source_was_is_a_conflict_too(project):
    """He replaced src/old.ts with a link. Moving it would move his link: refused, and it reads as changed."""
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").unlink()
    (project / "src" / "old.ts").symlink_to("../README.md")
    refused_as_a_conflict(project, "r1", "src/old.ts was changed in your project")
    assert os.readlink(project / "src" / "old.ts") == "../README.md" and not (project / "src" / "new.ts").exists()


def test_rn14_two_renames_one_clashing_write_nothing_not_even_a_mode(project):
    """The first rename's source is an executable; the second rename's destination is a file he made. Nothing moves:
    a refusal found halfway would roll the first back as a new file with mode 0644 and a new mtime."""
    (project / "src" / "a.ts").chmod(0o755)
    os.utime(project / "src" / "a.ts", ns=(LONG_AGO, LONG_AGO))
    local(project, "r1", several(move("src/a.ts", "src/a2.ts"), move("src/util.ts", "src/util2.ts")))
    (project / "src" / "util2.ts").write_text(HIS, encoding="utf-8")
    refused_as_a_conflict(project, "r1", "src/util2.ts was added to your project")
    assert (project / "src" / "a.ts").stat().st_mtime_ns == LONG_AGO and not (project / "src" / "a2.ts").exists()
    can_still_be_decided(project, "r1")


# --- Check 2: Keep never says ok for a destination it did not write ------------------------------------------------


def test_rn3_a_source_he_deleted_is_refused_and_the_destination_is_not_made_up(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").unlink()
    refused_as_a_conflict(project, "r1", "src/old.ts was removed from your project")
    assert not (project / "src" / "new.ts").exists()
    can_still_be_decided(project, "r1")
    assert history(project) == [("r1", "review")]


def test_rn6_a_source_he_moved_elsewhere_is_refused_and_stays_where_he_put_it(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").rename(project / "src" / "elsewhere.ts")
    refused_as_a_conflict(project, "r1", "src/old.ts was removed from your project")
    assert (project / "src" / "elsewhere.ts").read_text(encoding="utf-8") == MOVED
    assert not (project / "src" / "new.ts").exists()


def test_rn8_in_a_stack_a_deleted_source_refuses_the_keep_and_closes_no_older_request(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").unlink()
    local(project, "r2", write("src/b.ts", BLUE))
    refused_as_a_conflict(project, "r2", "src/old.ts was removed from your project")
    assert not (project / "src" / "new.ts").exists() and not (project / "src" / "b.ts").exists()
    assert history(project) == [("r2", "review"), ("r1", "review")]
    can_still_be_decided(project, "r2")


# --- Check 3: a move that is no clash still goes through -------------------------------------------------------


def test_rn2_a_destination_holding_exactly_the_requests_text_is_no_clash(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "new.ts").write_text(MOVED, encoding="utf-8")
    kept = keep_changes("r1", project)
    assert kept["ok"] is True and kept["applied"] == ["src/new.ts", "src/old.ts"]
    assert is_moved(project)
    assert history(project) == [("r1", "kept")]


def test_rn4_a_move_he_made_by_hand_still_goes_through_and_the_rest_lands(project):
    local(project, "r1", several(move("src/old.ts", "src/new.ts"), write("README.md", "# p\nrenamed old to new\n")))
    (project / "src" / "old.ts").rename(project / "src" / "new.ts")
    assert keep_changes("r1", project)["ok"] is True
    assert is_moved(project)
    assert (project / "README.md").read_text(encoding="utf-8") == "# p\nrenamed old to new\n"
    assert history(project) == [("r1", "kept")]


def test_rn4b_in_a_stack_a_move_he_made_by_hand_goes_through_and_closes_the_older_request(project):
    """R1 moves the file; he does the same move himself; R2 adds src/b.ts. Keep R2 takes it all: the pair is in the
    folder as R1 wants it, so R1 'went in with' R2 is true."""
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").rename(project / "src" / "new.ts")
    local(project, "r2", write("src/b.ts", BLUE))
    kept = keep_changes("r2", project)
    assert kept["ok"] is True and kept["also_kept"] == ["r1"]
    assert kept["applied"] == ["src/b.ts", "src/new.ts", "src/old.ts"]
    assert is_moved(project)
    assert (project / "src" / "b.ts").read_text(encoding="utf-8") == BLUE
    assert history(project) == [("r2", "kept"), ("r1", "kept")]
    assert snapshot("r1", repo=project)["summary"] == WENT_IN


def test_rn5_a_source_he_edited_is_refused_and_nothing_is_written(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").write_text("export const moved = 'EDITED BY NICO'\n", encoding="utf-8")
    before = state(project)
    assert refusal(project, "r1").code == "conflict"
    assert state(project) == before


def test_rn10_a_plain_move_in_a_stack_lands_and_his_unrelated_edit_survives(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    local(project, "r2", write("src/b.ts", BLUE))
    kept = keep_changes("r2", project)
    assert kept["ok"] is True and kept["also_kept"] == ["r1"]
    assert is_moved(project)
    assert (project / "src" / "b.ts").is_file()
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert history(project) == [("r2", "kept"), ("r1", "kept")]


def test_rn11_a_case_only_rename_goes_through_on_a_disk_that_ignores_case(project):
    (project / "src" / "Widget.ts").write_text("export const widget = 1\n", encoding="utf-8")
    local(project, "r1", move("src/Widget.ts", "src/widget.ts"))
    assert keep_changes("r1", project)["ok"] is True
    names = sorted(path.name for path in (project / "src").iterdir())
    assert "widget.ts" in names and "Widget.ts" not in names


def test_rn13_a_folder_at_the_destination_keeps_the_generic_refusal_and_writes_nothing(project):
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "new.ts").mkdir()
    (project / "src" / "new.ts" / "keep.txt").write_text("his\n", encoding="utf-8")
    before = state(project)
    error = refusal(project, "r1")
    assert error.code == "apply_failed" and error.detail == GENERIC
    assert state(project) == before


def test_rn12c_a_link_where_the_request_adds_a_file_is_refused_before_a_delete(project):
    """The request deletes an executable and adds src/new.ts, where he made a link. That refusal is not a clash he can
    see in a file list (the generic one), but the delete must not happen first."""
    (project / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (project / "run.sh").chmod(0o755)
    os.utime(project / "run.sh", ns=(LONG_AGO, LONG_AGO))
    local(project, "r1", several(remove("run.sh"), write("src/new.ts", MOVED)))
    (project / "src" / "new.ts").symlink_to("../README.md")
    before = state(project)
    error = refusal(project, "r1")
    assert error.code == "apply_failed" and error.detail == GENERIC
    assert state(project) == before and (project / "run.sh").stat().st_mtime_ns == LONG_AGO


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


def test_a_moved_file_he_edited_reads_as_changed_and_one_he_made_at_the_destination_as_added(project):
    """The two ends of a rename in one sentence: he edited the source and wrote his own destination."""
    local(project, "r1", move("src/old.ts", "src/new.ts"))
    (project / "src" / "old.ts").write_text("export const moved = 'EDITED BY NICO'\n", encoding="utf-8")
    (project / "src" / "new.ts").write_text(HIS, encoding="utf-8")
    refused_as_a_conflict(
        project, "r1", "src/old.ts was changed in your project and src/new.ts was added to your project",
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


# --- The hosted agent's real `bash` tool makes the rename (HM1 to HM4) --------------------------------------------


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A scripted hosted model: it answers in order, whatever it is sent."""

    def __init__(self, *replies: dict) -> None:
        self.replies = list(replies)

    def __call__(self, _messages: list[dict], _tools: list[dict]) -> dict:
        return self.replies.pop(0)


def hosted(project: Path, request_id: str, text: str, *replies: dict) -> dict:
    return run_general_request(
        project, text, request_id=request_id, agent_chat=Script(*replies), start_preview_after=False,
    )


def agent_moves(project: Path, request_id: str = "r1") -> dict:
    return hosted(
        project, request_id, "Rename old.ts to new.ts",
        reply(calls=[call("bash", {"command": "mv src/old.ts src/new.ts"}, "m")]), reply("Moved old.ts to new.ts."),
    )


def agent_adds(project: Path, request_id: str, path: str, body: str) -> dict:
    return hosted(
        project, request_id, f"Add {path}",
        reply(calls=[call("write", {"path": path, "content": body}, "w")]), reply("Added it."),
    )


@darwin_only
def test_hm1_the_agents_mv_then_a_file_he_made_at_the_destination_is_refused(project):
    review = agent_moves(project)
    assert review["facts"]["delta"]["renamed"] == [{"from": "src/old.ts", "to": "src/new.ts"}], "the real tool made it"
    (project / "src" / "new.ts").write_text(HIS, encoding="utf-8")
    refused_as_a_conflict(project, "r1", "src/new.ts was added to your project")
    assert (project / "src" / "new.ts").read_text(encoding="utf-8") == HIS


@darwin_only
def test_hm2_the_agents_mv_then_the_source_he_deleted_is_refused(project):
    agent_moves(project)
    (project / "src" / "old.ts").unlink()
    refused_as_a_conflict(project, "r1", "src/old.ts was removed from your project")
    assert not (project / "src" / "new.ts").exists() and history(project) == [("r1", "review")]


@darwin_only
def test_hm3_the_agents_plain_mv_in_a_stack_is_kept_with_the_follow_up(project):
    agent_moves(project)
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    agent_adds(project, "r2", "src/b.ts", BLUE)
    kept = keep_changes("r2", project)
    assert kept["ok"] is True and kept["also_kept"] == ["r1"]
    assert is_moved(project)
    assert (project / "src" / "b.ts").is_file() and (project / "README.md").read_text(encoding="utf-8") == HIS_README


@darwin_only
def test_hm4_a_follow_up_after_the_agents_mv_is_refused_when_he_made_the_destination(project):
    agent_moves(project)
    (project / "src" / "new.ts").write_text(HIS, encoding="utf-8")
    agent_adds(project, "r2", "src/b.ts", BLUE)
    refused_as_a_conflict(project, "r2", "src/new.ts was added to your project")
    assert (project / "src" / "new.ts").read_text(encoding="utf-8") == HIS and not (project / "src" / "b.ts").exists()
