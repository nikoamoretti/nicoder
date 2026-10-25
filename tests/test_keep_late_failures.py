"""Late failures: a Keep that is going to be refused for a reason it can know is refused before the first write.

Keep deletes first and writes after. A refusal found halfway is rolled back from the bytes, so an executable the request
deleted comes back as a new file (mode 0644, a new mtime), and a few shapes were not even refused: a looping link and a
folder he cannot search were an unhandled error ("Local Coder hit an unexpected problem"). Now everything Keep can know
is found first: a plain file or a link that is not a folder where a folder must be made, a folder where a file goes,
a folder where a hand-made move's source was, a looping link, a folder he cannot search. A parent that this Keep removes
or moves away itself is no obstacle. The sentence is the generic one, as for a link or a folder at a rename's
destination (RN12c, RN13); only a disk error is still found while writing.

Every scenario builds a project with an executable `run.sh` (0755, mtime far in the past), runs a request that
DELETES it and adds or moves something else, lets him put something in the way, and presses Keep through the helper's
RPC, as the app does. Names LTn / LGn / RLn are the cut's probe scenarios (LT the gaps, LG the guards, RL the named
limit). Every refusal compares the folder before and after: bytes, modes, mtimes, links and folders. Fake engines, $0.
"""
from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, snapshot
from local_coder.general import snapshot as snapshot_module
from local_coder.general.coordinator import list_requests, run_general_request

GENERIC = (
    "Local Coder could not write these changes into your project. "
    "Check that its folder and files can be changed, then try Keep again."
)
SH = "#!/bin/sh\necho hi\n"
MOVED = "export const moved = 'X'\n"
HIS = "his plain file\n"
LONG_AGO = 1_000_000_000_000_000_000  # ns: any rewrite of a file moves its mtime off this


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(root / ".local" / "share" / "local-coder"))
    return root


@pytest.fixture
def project(home) -> Path:
    """A small app with an executable run.sh that no request may rewrite."""
    root = home / "projects" / "app"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# p\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"p"}\n', encoding="utf-8")
    (root / "src" / "old.ts").write_text(MOVED, encoding="utf-8")
    (root / "run.sh").write_text(SH, encoding="utf-8")
    (root / "run.sh").chmod(0o755)
    os.utime(root / "run.sh", ns=(LONG_AGO, LONG_AGO))
    return root


# --- What the scenarios do and look at ---------------------------------------------------------------------------


def local(project: Path, request_id: str, engine) -> dict:
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd),
    )


def deletes_run_sh_and(*steps):
    def engine(cwd: Path) -> None:
        (cwd / "run.sh").unlink()
        for step in steps:
            step(cwd)

    return engine


def adds(path: str, body: str = "X\n"):
    def engine(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def moves(source: str, destination: str):
    def engine(cwd: Path) -> None:
        (cwd / destination).parent.mkdir(parents=True, exist_ok=True)
        (cwd / source).rename(cwd / destination)

    return engine


def several(*steps):
    def engine(cwd: Path) -> None:
        for step in steps:
            step(cwd)

    return engine


def stamps(root: Path) -> dict[str, tuple]:
    """Everything under root: bytes, mode and mtime of each file, the target of each link, each folder."""
    found: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        if path.is_symlink():
            found[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            found[rel] = ("folder",)
        else:
            found[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes(), path.stat().st_mtime_ns)
    return found


def keep(project: Path, request_id: str) -> dict:
    """Press Keep through the helper's RPC: {"ok": True, "result": ...} or {"ok": False, "error": {code, detail}}."""
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    return server.dispatch({"id": "1", "method": "keep", "params": {"episode_id": request_id, "path": str(project)}})


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def run_sh_is_untouched(project: Path) -> bool:
    exe = project / "run.sh"
    return exe.is_file() and stat.S_IMODE(exe.stat().st_mode) == 0o755 and exe.stat().st_mtime_ns == LONG_AGO


def refused_before_writing(project: Path, request_id: str, before: dict, *, locked: Path | None = None) -> None:
    """Keep is refused as `apply_failed` with the plain sentence (never `internal`), the folder is byte-identical, modes
    and mtimes included, run.sh is untouched, and the request can still be decided."""
    if locked is not None:
        locked.chmod(0o000)
    try:
        reply = keep(project, request_id)
    finally:
        if locked is not None:
            locked.chmod(0o755)
    assert reply["ok"] is False, reply
    assert reply["error"]["code"] == "apply_failed" and reply["error"]["detail"] == GENERIC, reply["error"]
    assert stamps(project) == before, "a refused Keep writes nothing"
    assert run_sh_is_untouched(project), "run.sh is still 0755 with its old mtime"
    review = snapshot(request_id, repo=project)
    assert review["can_keep"] is True and review["can_discard"] is True and review["outcome"] == "review"


# === The gaps: refused before the first write ======================================================================


def test_lt1_a_file_where_a_folder_must_be_made(project):
    """The request deletes run.sh and adds lib/x.ts; he makes a plain FILE named lib."""
    local(project, "r1", deletes_run_sh_and(adds("lib/x.ts")))
    (project / "lib").write_text(HIS, encoding="utf-8")
    refused_before_writing(project, "r1", stamps(project))
    assert (project / "lib").is_file() and (project / "lib").read_text(encoding="utf-8") == HIS


@pytest.mark.parametrize("empty", [False, True], ids=["lt2_with_a_file_in_it", "lt2b_empty"])
def test_lt2_a_folder_where_an_added_file_goes(project, empty):
    """The request adds b.ts; he makes a FOLDER named b.ts (the snapshot lists files only, so even an empty one is no
    clash)."""
    local(project, "r1", deletes_run_sh_and(adds("b.ts", "B\n")))
    (project / "b.ts").mkdir()
    if not empty:
        (project / "b.ts" / "his.txt").write_text("his\n", encoding="utf-8")
    refused_before_writing(project, "r1", stamps(project))
    assert (project / "b.ts").is_dir()


def test_lt3_a_hand_made_move_with_a_folder_at_the_source(project):
    local(project, "r1", deletes_run_sh_and(moves("src/old.ts", "src/new.ts")))
    (project / "src" / "old.ts").rename(project / "src" / "new.ts")  # he made the same move...
    (project / "src" / "old.ts").mkdir()  # ...and put a folder where the file was
    refused_before_writing(project, "r1", stamps(project))
    assert (project / "src" / "old.ts").is_dir()


def test_lt4_a_rename_into_a_new_folder_with_his_file_in_the_way(project):
    local(project, "r1", deletes_run_sh_and(moves("src/old.ts", "lib/new.ts")))
    (project / "lib").write_text(HIS, encoding="utf-8")
    refused_before_writing(project, "r1", stamps(project))
    assert (project / "src" / "old.ts").read_text(encoding="utf-8") == MOVED, "the move never started"


def test_lt5_a_dangling_link_as_the_parent_folder(project):
    local(project, "r1", deletes_run_sh_and(adds("d/a.ts", "A\n")))
    (project / "d").symlink_to("nowhere-at-all")
    refused_before_writing(project, "r1", stamps(project))
    assert os.readlink(project / "d") == "nowhere-at-all"


def test_lt6_a_link_that_loops_at_an_added_path(project):
    """Was "Local Coder hit an unexpected problem" (code `internal`): `resolve()` raises RuntimeError on a loop."""
    local(project, "r1", deletes_run_sh_and(adds("a.ts", "A\n")))
    (project / "a.ts").symlink_to("a.ts")
    refused_before_writing(project, "r1", stamps(project))
    assert os.readlink(project / "a.ts") == "a.ts"


def test_lt7_a_link_that_loops_as_the_parent_folder(project):
    local(project, "r1", deletes_run_sh_and(adds("d/a.ts", "A\n")))
    (project / "d").symlink_to("d")
    refused_before_writing(project, "r1", stamps(project))
    assert os.readlink(project / "d") == "d"


def test_lt8_in_a_stack_a_refused_keep_closes_nothing_and_leaves_run_sh_alone(project):
    """R1 deletes run.sh; R2 adds lib/x.ts; he makes a FILE named lib. Keep R2 is refused: R1 stays open, and run.sh
    (R1's delete) was never touched."""
    local(project, "r1", lambda cwd: (cwd / "run.sh").unlink())
    local(project, "r2", adds("lib/x.ts"))
    (project / "lib").write_text(HIS, encoding="utf-8")
    before = stamps(project)
    reply = keep(project, "r2")
    assert reply["ok"] is False and reply["error"]["code"] == "apply_failed" and reply["error"]["detail"] == GENERIC
    assert stamps(project) == before and run_sh_is_untouched(project)
    assert history(project) == [("r2", "review"), ("r1", "review")], "nothing was closed"


def test_lt9_a_file_two_folders_up(project):
    local(project, "r1", deletes_run_sh_and(adds("a/b/c.ts", "C\n")))
    (project / "a").write_text(HIS, encoding="utf-8")
    refused_before_writing(project, "r1", stamps(project))
    assert (project / "a").is_file()


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any folder")
def test_lt10_a_folder_he_cannot_search(project):
    """Was an unhandled PermissionError (code `internal`): the check meets it too, so all of it is inside the `try`."""
    (project / "sub").mkdir()
    (project / "sub" / "keep.txt").write_text("k\n", encoding="utf-8")
    local(project, "r1", deletes_run_sh_and(adds("sub/x.ts", "X\n")))
    refused_before_writing(project, "r1", stamps(project), locked=project / "sub")
    assert not (project / "sub" / "x.ts").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any folder")
def test_lt10b_a_rename_inside_a_folder_he_cannot_search(project):
    """Was code `internal` too: the clash check looks at a rename's two ends on the disk, inside the folder, before the
    pre-flight starts (QA's repro). The request moves src/old.ts to src/new.ts; src is then made unsearchable."""
    local(project, "r1", deletes_run_sh_and(moves("src/old.ts", "src/new.ts")))
    refused_before_writing(project, "r1", stamps(project), locked=project / "src")
    assert (project / "src" / "old.ts").read_text(encoding="utf-8") == MOVED
    assert not (project / "src" / "new.ts").exists()


# === The guards: a Keep that can go through still does ============================================================


def test_lg1_folders_that_do_not_exist_yet_are_made(project):
    local(project, "r1", deletes_run_sh_and(adds("deep/er/x.ts", "X\n")))
    reply = keep(project, "r1")
    assert reply["ok"] is True and reply["result"]["applied"] == ["deep/er/x.ts", "run.sh"]
    assert (project / "deep" / "er" / "x.ts").read_text(encoding="utf-8") == "X\n" and not (project / "run.sh").exists()


def test_lg2_an_existing_empty_folder_is_a_fine_parent(project):
    local(project, "r1", deletes_run_sh_and(adds("lib/x.ts", "X\n")))
    (project / "lib").mkdir()
    assert keep(project, "r1")["ok"] is True
    assert (project / "lib" / "x.ts").read_text(encoding="utf-8") == "X\n"


def test_lg3_a_move_he_made_by_hand_still_goes_through(project):
    local(project, "r1", deletes_run_sh_and(moves("src/old.ts", "src/new.ts")))
    (project / "src" / "old.ts").rename(project / "src" / "new.ts")
    assert keep(project, "r1")["ok"] is True
    assert (project / "src" / "new.ts").read_text(encoding="utf-8") == MOVED
    assert not (project / "src" / "old.ts").exists() and not (project / "run.sh").exists()


def test_lg4_a_plain_clash_is_still_a_conflict_that_names_the_file(project):
    local(project, "r1", deletes_run_sh_and(adds("src/b.ts", "B\n")))
    (project / "src" / "b.ts").write_text("export const b = 'NICO'\n", encoding="utf-8")
    before = stamps(project)
    reply = keep(project, "r1")
    assert reply["ok"] is False and reply["error"]["code"] == "conflict"
    assert reply["error"]["detail"] == (
        "Not kept: since these changes were started, src/b.ts was added to your project. "
        "Your project was not changed."
    )
    assert stamps(project) == before


def test_lg5_a_link_at_an_added_path_is_still_refused_before_any_write(project):
    local(project, "r1", deletes_run_sh_and(adds("src/new.ts", MOVED)))
    (project / "src" / "new.ts").symlink_to("../README.md")
    refused_before_writing(project, "r1", stamps(project))
    assert os.readlink(project / "src" / "new.ts") == "../README.md"


def test_lg6_a_link_to_a_folder_inside_the_project_is_a_parent_as_before(project):
    local(project, "r1", deletes_run_sh_and(adds("d/a.ts", "A\n")))
    (project / "real").mkdir()
    (project / "d").symlink_to("real")
    assert keep(project, "r1")["ok"] is True
    assert (project / "real" / "a.ts").read_text(encoding="utf-8") == "A\n", "the write went through the link"


def delete_then_add(project: Path, request_id: str, old: str, new_files: dict[str, str]):
    """A request that replaces the file `old` by new files (the engine deletes it, then writes them)."""

    def engine(cwd: Path) -> None:
        (cwd / old).unlink()
        for name, body in new_files.items():
            (cwd / name).parent.mkdir(parents=True, exist_ok=True)
            (cwd / name).write_text(body, encoding="utf-8")

    return local(project, request_id, engine)


def test_lg7a_a_file_replaced_by_a_folder_of_files_goes_through(project):
    """`config` (a plain file) becomes `config/` with two files in it: the file is deleted before the folder is made."""
    (project / "config").write_text("A=1\n", encoding="utf-8")
    delete_then_add(project, "r1", "config", {"config/dev.json": "{}\n", "config/prod.json": '{"p": 1}\n'})
    reply = keep(project, "r1")
    assert reply["ok"] is True, reply
    assert reply["result"]["applied"] == ["config", "config/dev.json", "config/prod.json"]
    assert (project / "config").is_dir() and (project / "config" / "dev.json").read_text(encoding="utf-8") == "{}\n"


def test_lg7b_a_script_replaced_by_a_folder_with_one_inside_goes_through(project):
    (project / "scripts").mkdir()
    (project / "scripts" / "deploy").write_text(SH, encoding="utf-8")
    delete_then_add(project, "r1", "scripts/deploy", {"scripts/deploy/run.sh": SH.replace("hi", "hello")})
    reply = keep(project, "r1")
    assert reply["ok"] is True, reply
    assert reply["result"]["applied"] == ["scripts/deploy", "scripts/deploy/run.sh"]
    assert (project / "scripts" / "deploy").is_dir()


def test_lg7c_a_file_moved_away_and_a_folder_made_where_it_was_goes_through(project):
    """`lib` is renamed to `lib.old` and a new `lib/x.ts` is added: the move comes before the folder is made."""
    (project / "lib").write_text("text\n", encoding="utf-8")
    local(project, "r1", several(moves("lib", "lib.old"), adds("lib/x.ts", "new\n")))
    reply = keep(project, "r1")
    assert reply["ok"] is True, reply
    assert reply["result"]["applied"] == ["lib", "lib.old", "lib/x.ts"]
    assert (project / "lib.old").read_text(encoding="utf-8") == "text\n"
    assert (project / "lib" / "x.ts").read_text(encoding="utf-8") == "new\n"


def test_lg7e_a_file_replaced_by_a_folder_that_differs_only_in_case_goes_through(project):
    """macOS does not tell `Docs` from `docs`: the file is deleted and the folder made, as it always was."""
    probe = project / "case-probe"
    probe.write_text("x", encoding="utf-8")
    ignores_case = (project / "CASE-PROBE").exists()
    probe.unlink()
    if not ignores_case:
        pytest.skip("this disk tells Docs from docs, so there is no such replacement to make")
    (project / "Docs").write_text("A file named Docs\n", encoding="utf-8")
    delete_then_add(project, "r1", "Docs", {"docs/readme.md": "# docs\n"})
    reply = keep(project, "r1")
    assert reply["ok"] is True, reply
    assert (project / "docs").is_dir() and (project / "docs" / "readme.md").read_text(encoding="utf-8") == "# docs\n"


# === The named limit: what Keep cannot know is still found while writing =========================================


def test_rl1_a_disk_error_found_only_while_writing_is_still_refused_and_rolled_back(project, monkeypatch):
    """The second write fails: Keep is refused as `apply_failed`, everything is put back from the bytes. Named limit (a
    later cut, `rollback-keeps-modes`): the deleted run.sh comes back as a new file, so its mode and mtime are not
    asserted here; this cut must not make it worse."""
    local(project, "r1", deletes_run_sh_and(adds("src/x.ts", "X\n"), adds("src/y.ts", "Y\n")))
    real, calls = snapshot_module._atomic_copy, {"n": 0}

    def fails_on_the_second(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device", str(dst))
        real(src, dst)

    monkeypatch.setattr(snapshot_module, "_atomic_copy", fails_on_the_second)
    reply = keep(project, "r1")
    assert reply["ok"] is False and reply["error"]["code"] == "apply_failed" and reply["error"]["detail"] == GENERIC
    assert (project / "run.sh").read_text(encoding="utf-8") == SH
    assert not (project / "src" / "x.ts").exists() and not (project / "src" / "y.ts").exists()
    assert snapshot("r1", repo=project)["can_keep"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any folder")
def test_rl2_a_read_only_folder_is_a_disk_error_found_only_while_writing(project):
    """Keep does not guess permissions: the write fails and is rolled back from the bytes (the same named limit)."""
    (project / "sub").mkdir()
    (project / "sub" / "keep.txt").write_text("k\n", encoding="utf-8")
    local(project, "r1", deletes_run_sh_and(adds("sub/x.ts", "X\n")))
    (project / "sub").chmod(0o555)
    try:
        reply = keep(project, "r1")
    finally:
        (project / "sub").chmod(0o755)
    assert reply["ok"] is False and reply["error"]["code"] == "apply_failed" and reply["error"]["detail"] == GENERIC
    assert (project / "run.sh").read_text(encoding="utf-8") == SH and not (project / "sub" / "x.ts").exists()
    assert (project / "sub" / "keep.txt").read_text(encoding="utf-8") == "k\n"
