"""Cut net-out: a request that a later Keep took files back from is closed, and can never be kept on its own.

A request built on an older one that still waits carries the older result: keeping the newer one puts the older
changes into the project too, minus whatever the newer one took back. The old rule closed the older request only when
the Keep had written every one of its files. A later request that removed what the earlier one added, or put back what
it fixed, left the older request open with a Keep that could only undo what he had just kept (the file came back, the
fix was redone) or fail with a sentence that blamed him for what the app's own Keep did.

Now Keep closes every waiting request the kept one was built on, nearest first, and records which of their files the
Keep did not write (`dropped`: the request's files minus the Keep's `applied`; per file, never by text). All carried:
the words are the ones Nico already saw. Some taken back: History says `replaced`, the card and the refusals say
"These changes were replaced by a later request you kept. Nothing from them went into <files>." The walk stops at the
first request with no files (a request that changed nothing published its starting copy, so a request that continues
it does not carry what lies below it). A request that is still open in its record although a kept one was built on it
(an older build, or a record that could not be saved) is read as closed from the kept request's record; nothing is
rewritten. After QA's second round (x1, x2): a result that lists no files while its baseline lists some (a
`candidate/` that was emptied or made unreadable, or a request that deleted every file) has no files to name, so the
request is closed as carried with the plain words, never as replaced with its whole baseline.

Every test is modelled on a scenario of the cut's probe (NT* the gaps, NG* the guards, NR* the named limit) and names
the acceptance check it belongs to. Fake engines and a scripted hosted agent ($0); the workspace, baselines, Keep,
Discard, History and the helper's RPC dispatch are the real code. Every refusal compares the folder before and after,
file modes included: a refused Keep or Discard writes nothing.
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
from local_coder.general import coordinator
from local_coder.general.coordinator import (
    GeneralError,
    list_requests,
    load_meta,
    project_id_for,
    request_dir,
    run_general_request,
)
from local_coder.general.waiting import find_waiting_result, waiting_note

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo_calc" / "repo"
BUG = "return a - b  # BUG: subtracts instead of adding"
FIXED = "return a + b"
WENT_IN = "These changes went in with a later request you kept."
ALREADY_KEPT = "These changes are already in your project."
ALREADY_KEPT_DISCARD = "These changes are already in your project, so they cannot be discarded."
REPLACED = "These changes were replaced by a later request you kept. Nothing from them went into "

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="the agent's bash runs in sandbox-exec: macOS only")


def replaced(files: str) -> str:
    """The sentence for a replaced request; `files` is the tail as Nico reads it, e.g. "src/gen.py"."""
    return f"{REPLACED}{files}."


GEN = replaced("src/gen.py")
CALC = replaced("src/calc.py")


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
    """A copy of the demo calculator: `add` subtracts, three tests."""
    root = home / "projects" / "calc"
    shutil.copytree(FIXTURE, root)
    return root


# --- Engines: what a request does to its working copy -----------------------------------------------------------


def local(project: Path, request_id: str, engine, **kwargs) -> dict:
    """A request on the local path with a fake engine (Keep is one code path for every engine)."""
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd), **kwargs,
    )


def write(path: str, body: str):
    def engine(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def remove(path: str):
    return lambda cwd: (cwd / path).unlink()


def move(source: str, destination: str):
    def engine(cwd: Path) -> None:
        (cwd / destination).parent.mkdir(parents=True, exist_ok=True)
        (cwd / source).rename(cwd / destination)

    return engine


def both(*engines):
    def engine(cwd: Path) -> None:
        for step in engines:
            step(cwd)

    return engine


def nothing(_cwd: Path) -> None:
    """An engine that changes nothing: the request ends with no files of its own."""


def calc_fix(cwd: Path) -> None:
    calc = cwd / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace(BUG, FIXED), encoding="utf-8")


def calc_bug(cwd: Path) -> None:
    calc = cwd / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace(FIXED, BUG), encoding="utf-8")


def calc_append(cwd: Path) -> None:
    calc = cwd / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8") + "\n# documented\n", encoding="utf-8")


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def hosted(project: Path, request_id: str, *operations: tuple) -> dict:
    """A request on the hosted agent whose scripted model makes these edits: ("write", path, text) or
    ("edit", path, old, new) through the real tools."""
    calls = []
    for number, step in enumerate(operations):
        if step[0] == "write":
            calls.append(call("write", {"path": step[1], "content": step[2]}, f"c{number}"))
        else:
            calls.append(call("edit", {"path": step[1], "old_string": step[2], "new_string": step[3]}, f"c{number}"))
    steps = iter([reply(calls=calls), reply("Done.")])
    return run_general_request(project, f"request {request_id}", request_id=request_id,
                               agent_chat=lambda _messages, _tools: next(steps), start_preview_after=False)


def bash(project: Path, request_id: str, command: str) -> dict:
    """A hosted request whose agent runs one command in its sandbox: the only way it deletes a file."""
    steps = iter([reply(calls=[call("bash", {"command": command, "description": "run it"}, "sh")]), reply("Done.")])
    return run_general_request(project, f"request {request_id}", request_id=request_id,
                               agent_chat=lambda _messages, _tools: next(steps), start_preview_after=False)


# --- What the scenarios look at -----------------------------------------------------------------------------------


def state(root: Path) -> dict[str, tuple]:
    """Everything under root: bytes and mode of each file, the target of each link, each folder."""
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
            found[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return found


def card(project: Path, request_id: str) -> dict:
    """The parts of a snapshot the cut pins."""
    review = snapshot(request_id, repo=project)
    return {
        "can_keep": review["can_keep"], "can_discard": review["can_discard"], "outcome": review["outcome"],
        "blocked_by": review.get("blocked_by"), "kept_with": review.get("kept_with"),
        "dropped": review.get("dropped") or [],
        "files": review["facts"]["files"], "summary": review["summary"],
    }


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def refusal(action) -> tuple[str, str]:
    """(code, sentence) an action was refused with."""
    with pytest.raises(GeneralError) as raised:
        action()
    return raised.value.code, raised.value.detail


def keep_refusal(project: Path, request_id: str) -> tuple[str, str]:
    return refusal(lambda: keep_changes(request_id, project))


def discard_refusal(project: Path, request_id: str) -> tuple[str, str]:
    return refusal(lambda: discard_changes(request_id, repo=project))


def assert_closed_as_replaced(project: Path, request_id: str, by: str, files: list[str], sentence: str) -> None:
    """The request is closed because a later Keep took some of it back: nothing to press, the true words, final."""
    seen = card(project, request_id)
    assert seen["can_keep"] is False and seen["can_discard"] is False and seen["blocked_by"] is None
    assert seen["outcome"] == "replaced" and seen["kept_with"] == by
    assert seen["dropped"] == files and seen["summary"] == sentence
    assert seen["files"], "what it changed is still described"
    before = state(project)
    assert keep_refusal(project, request_id) == ("kept", sentence)
    assert discard_refusal(project, request_id) == ("kept", sentence)
    assert state(project) == before, "a refused Keep or Discard writes nothing"


def assert_closed_as_went_in(project: Path, request_id: str, by: str) -> None:
    """The request is closed and every file of it went in: the words are the ones that were always there."""
    seen = card(project, request_id)
    assert seen["can_keep"] is False and seen["can_discard"] is False
    assert seen["outcome"] == "kept" and seen["kept_with"] == by and seen["dropped"] == []
    assert seen["summary"] == WENT_IN
    assert keep_refusal(project, request_id) == ("kept", ALREADY_KEPT)
    assert discard_refusal(project, request_id) == ("kept", ALREADY_KEPT_DISCARD)


def nothing_waits(project: Path) -> bool:
    return find_waiting_result(project) is None and waiting_note(project, can_read=True) == ""


# === Check 1: files a later Keep took back stay out, whatever the shape ==========================================


def test_nt1_a_file_the_later_request_removed_does_not_come_back(project):
    """NO2: R1 adds gen.py; R2 removes it and adds other.py. Keep R2, then R1 (which used to bring gen.py back)."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    assert load_meta(project_id_for(project), "r2")["built_on"] == "r1"
    assert card(project, "r2")["files"] == ["src/other.py"]
    held = card(project, "r1")
    assert held["can_keep"] is False and held["blocked_by"] == "r2", "while R2 waits, R1 is held back as before"
    kept = keep_changes("r2", project)
    assert kept["ok"] is True and kept["applied"] == ["src/other.py"] and kept["also_kept"] == ["r1"]
    assert not (project / "src" / "gen.py").exists()
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]
    assert nothing_waits(project), "a question is not told that r1 waits, not in the folder yet"
    assert not (project / "src" / "gen.py").exists()


def test_nt2_a_fix_the_later_request_put_back_is_not_redone(project):
    """NO3: R1 fixes calc.py and adds gen.py; R2 puts the bug back (he asked) and adds other.py."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", both(calc_bug, write("src/other.py", "OTHER = 1\n")))
    assert card(project, "r2")["files"] == ["src/gen.py", "src/other.py"], "calc.py is back to its baseline"
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/gen.py", "src/other.py"] and kept["also_kept"] == ["r1"]
    assert BUG in (project / "src" / "calc.py").read_text(encoding="utf-8")
    assert_closed_as_replaced(project, "r1", "r2", ["src/calc.py"], CALC)
    assert BUG in (project / "src" / "calc.py").read_text(encoding="utf-8"), "the fix is not redone over the bug"
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]


def test_nt3_a_shared_file_and_a_removed_one_are_no_clash_that_blames_him(project):
    """NO1: R2 appends to calc.py (which R1 fixed) and removes gen.py. Keep R1 used to be refused as a clash."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", both(calc_append, remove("src/gen.py")))
    assert card(project, "r2")["files"] == ["src/calc.py"]
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    text = (project / "src" / "calc.py").read_text(encoding="utf-8")
    assert FIXED in text and "# documented" in text and not (project / "src" / "gen.py").exists()
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]


def test_nt5_the_same_through_the_hosted_agent(project):
    """NT2 with the scripted agent's real write and edit tools: both requests are agent runs."""
    hosted(project, "r1", ("edit", "src/calc.py", BUG, FIXED), ("write", "src/gen.py", "GEN = 1\n"))
    hosted(project, "r2", ("edit", "src/calc.py", FIXED, BUG), ("write", "src/other.py", "OTHER = 1\n"))
    pid = project_id_for(project)
    assert [load_meta(pid, rid)["engine"] for rid in ("r1", "r2")] == ["agent", "agent"]
    assert load_meta(pid, "r2")["built_on"] == "r1"
    assert card(project, "r2")["files"] == ["src/gen.py", "src/other.py"]
    assert keep_changes("r2", project)["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r2", ["src/calc.py"], CALC)
    assert BUG in (project / "src" / "calc.py").read_text(encoding="utf-8")
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]


def test_nt6_a_rename_the_later_request_undid_does_not_come_back(project):
    """R1 renames calc.py to calculator.py; R2 renames it back and adds other.py."""
    local(project, "r1", move("src/calc.py", "src/calculator.py"))
    local(project, "r2", both(move("src/calculator.py", "src/calc.py"), write("src/other.py", "OTHER = 1\n")))
    assert card(project, "r1")["files"] == ["src/calc.py", "src/calculator.py"]
    assert card(project, "r2")["files"] == ["src/other.py"]
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r2", ["src/calc.py", "src/calculator.py"],
                              replaced("src/calc.py or src/calculator.py"))
    assert (project / "src" / "calc.py").exists() and not (project / "src" / "calculator.py").exists()


def test_nt12_a_continuation_that_removes_the_file_closes_the_request_it_continues(project):
    """R2 continues R1 (`continue_from`, its own baseline) and removes R1's gen.py."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", remove("src/gen.py"), continue_from="r1")
    meta = load_meta(project_id_for(project), "r2")
    assert meta["built_on"] == "r1" and meta["continued_from"] == "r1"
    assert card(project, "r2")["files"] == ["src/calc.py"]
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    assert not (project / "src" / "gen.py").exists()


@darwin_only
def test_bash_b_the_agents_rm_of_a_file_the_earlier_request_made_does_not_bring_it_back(project):
    """The hosted agent's only way to delete: its real sandboxed `bash`. R1 creates gen.py, R2 removes it."""
    bash(project, "r1", "printf 'GEN = 1\\n' > src/gen.py && ls src")
    bash(project, "r2", "rm src/gen.py && printf 'OTHER = 1\\n' > src/other.py && ls src")
    assert load_meta(project_id_for(project), "r2")["built_on"] == "r1"
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    assert not (project / "src" / "gen.py").exists()
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]


# === Check 2: a chain of three is closed all the way ==============================================================


def test_nt4_a_chain_of_three_is_closed_all_the_way(project):
    """R1 fixes calc.py and adds gen.py; R2 adds extra.py; R3 removes gen.py. Keep R3 closes R2 and R1."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    local(project, "r3", remove("src/gen.py"))
    pid = project_id_for(project)
    assert [load_meta(pid, rid).get("built_on") for rid in ("r1", "r2", "r3")] == [None, "r1", "r2"]
    assert card(project, "r3")["files"] == ["src/calc.py", "src/extra.py"]
    kept = keep_changes("r3", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["r2", "r1"]
    assert not (project / "src" / "gen.py").exists()
    assert_closed_as_replaced(project, "r2", "r3", ["src/gen.py"], GEN)
    assert_closed_as_replaced(project, "r1", "r3", ["src/gen.py"], GEN)
    assert history(project) == [("r3", "kept"), ("r2", "replaced"), ("r1", "replaced")]
    assert not (project / "src" / "gen.py").exists()


@darwin_only
def test_bash_a_a_chain_of_three_through_the_agents_real_bash(project):
    """QA's stack: R1 removes old.py, makes gen.py and fixes calc.py; he writes notes.txt; R2 lists and adds extra.py;
    R3 removes gen.py. Keep R3 applies the net change and closes R2 and R1."""
    (project / "src" / "old.py").write_text("OLD = 1\n", encoding="utf-8")
    steps = iter([
        reply(calls=[call("bash", {"command": "rm src/old.py && printf 'GEN = 1\\n' > src/gen.py && ls src"}, "sh")]),
        reply(calls=[call("edit", {"path": "src/calc.py", "old_string": BUG, "new_string": FIXED}, "e")]),
        reply("Removed old.py, added gen.py, fixed add()."),
    ])
    run_general_request(project, "Clean up and fix add", request_id="r1",
                        agent_chat=lambda _m, _t: next(steps), start_preview_after=False)
    (project / "notes.txt").write_text("Nico's notes\n", encoding="utf-8")
    steps = iter([
        reply(calls=[call("bash", {"command": "ls src"}, "ls")]),
        reply(calls=[call("write", {"path": "src/extra.py", "content": "EXTRA = 1\n"}, "w")]),
        reply("Added extra.py."),
    ])
    run_general_request(project, "Now also add an extra module", request_id="r2",
                        agent_chat=lambda _m, _t: next(steps), start_preview_after=False)
    bash(project, "r3", "rm src/gen.py && ls src")
    pid = project_id_for(project)
    assert [load_meta(pid, rid).get("built_on") for rid in ("r2", "r3")] == ["r1", "r2"]
    assert card(project, "r3")["files"] == ["src/calc.py", "src/extra.py", "src/old.py"]
    kept = keep_changes("r3", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py", "src/old.py"] and kept["also_kept"] == ["r2", "r1"]
    assert not (project / "src" / "old.py").exists() and not (project / "src" / "gen.py").exists()
    calc = (project / "src" / "calc.py").read_text(encoding="utf-8")
    assert (project / "src" / "extra.py").is_file() and FIXED in calc
    assert (project / "notes.txt").read_text(encoding="utf-8") == "Nico's notes\n"
    assert_closed_as_replaced(project, "r2", "r3", ["src/gen.py"], GEN)
    assert_closed_as_replaced(project, "r1", "r3", ["src/gen.py"], GEN)
    assert history(project) == [("r3", "kept"), ("r2", "replaced"), ("r1", "replaced")]


# === Check 3: his edits survive, and a refused Keep closes nothing ================================================


def test_nt7a_his_own_file_at_a_dropped_path_and_his_other_edits_survive(project):
    """He writes his own src/gen.py and a README while R1 (adds gen.py) and R2 (removes it) wait."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    (project / "src" / "gen.py").write_text("MINE = 1\n", encoding="utf-8")
    (project / "README.md").write_text("Nico's own notes\n", encoding="utf-8")
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == ["r1"]
    assert (project / "src" / "gen.py").read_text(encoding="utf-8") == "MINE = 1\n"
    assert (project / "README.md").read_text(encoding="utf-8") == "Nico's own notes\n"
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    assert (project / "src" / "gen.py").read_text(encoding="utf-8") == "MINE = 1\n"


def test_nt7b_a_keep_refused_for_his_edit_closes_nothing(project):
    """He edited calc.py, which R1 and R2 both changed: Keep R2 is a clash, nothing is closed, R2 can be discarded."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", both(calc_append, remove("src/gen.py")))
    calc = project / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8") + "\n# nico\n", encoding="utf-8")
    before = state(project)
    code, detail = keep_refusal(project, "r2")
    assert code == "conflict" and "src/calc.py was changed in your project" in detail
    assert state(project) == before
    r1, r2 = card(project, "r1"), card(project, "r2")
    assert r1["outcome"] == "review" and r1["can_keep"] is False and r1["blocked_by"] == "r2", "nothing was closed"
    assert r2["can_keep"] is True and r2["outcome"] == "review"
    assert history(project) == [("r2", "review"), ("r1", "review")]
    assert discard_changes("r2", repo=project)["discarded"] is True
    assert card(project, "r1")["can_keep"] is True, "R1 is free again"
    code, detail = keep_refusal(project, "r1")
    assert code == "conflict" and "src/calc.py was changed in your project" in detail, "his edit, still a clash"
    assert state(project) == before


# === Check 4: Discard and Keep stay final; nothing comes back ======================================================


def test_nt8c_a_discarded_request_in_between_stays_discarded(project):
    """R1 fixes calc.py and adds gen.py; R2 adds extra.py and is discarded; R3 (built on R1) removes gen.py."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    assert discard_changes("r2", repo=project)["discarded"] is True
    local(project, "r3", remove("src/gen.py"))
    assert load_meta(project_id_for(project), "r3")["built_on"] == "r1", "the discarded R2 is passed over"
    kept = keep_changes("r3", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r3", ["src/gen.py"], GEN)
    assert card(project, "r2")["outcome"] == "discarded"
    assert not (project / "src" / "extra.py").exists() and not (project / "src" / "gen.py").exists()
    assert history(project) == [("r3", "kept"), ("r2", "discarded"), ("r1", "replaced")]


def test_nt8d_an_idle_request_in_between_keeps_its_word(project):
    """The same with an idle R2 (its engine writes nothing): R3 is built on R1 and R2 stays idle."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", nothing)
    local(project, "r3", remove("src/gen.py"))
    assert load_meta(project_id_for(project), "r3")["built_on"] == "r1", "the idle R2 is passed over"
    assert keep_changes("r3", project)["also_kept"] == ["r1"]
    assert_closed_as_replaced(project, "r1", "r3", ["src/gen.py"], GEN)
    idle = card(project, "r2")
    assert idle["files"] == [] and idle["can_keep"] is False and idle["can_discard"] is False
    assert idle["outcome"] == "review" and idle["kept_with"] is None


def test_nt9_discard_after_the_keep_is_refused_with_the_true_sentence(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    keep_changes("r2", project)
    before = state(project)
    assert discard_refusal(project, "r1") == ("kept", GEN)
    assert discard_refusal(project, "r2") == ("kept", ALREADY_KEPT_DISCARD)
    assert state(project) == before
    assert card(project, "r1")["outcome"] == "replaced"


def test_ng2_discarding_the_newer_request_frees_the_older_one_and_brings_nothing_back(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    assert discard_changes("r2", repo=project)["discarded"] is True
    freed = card(project, "r1")
    assert freed["can_keep"] is True and freed["blocked_by"] is None
    kept = keep_changes("r1", project)
    assert kept["applied"] == ["src/gen.py"] and kept["also_kept"] == []
    assert (project / "src" / "gen.py").is_file() and not (project / "src" / "other.py").exists()
    assert keep_refusal(project, "r2")[0] == "discarded"
    assert history(project) == [("r2", "discarded"), ("r1", "kept")]


# === Check 5: the words and the fields ============================================================================


@pytest.mark.parametrize(
    ("count", "tail"),
    [
        (2, "src/g1.py or src/g2.py."),
        (4, "src/g1.py, src/g2.py, src/g3.py or 1 more file."),
        (5, "src/g1.py, src/g2.py, src/g3.py or 2 more files."),
    ],
)
def test_nt11_the_sentence_names_at_most_three_files_then_counts_the_rest(project, count, tail):
    names = [f"src/g{number}.py" for number in range(1, count + 1)]
    local(project, "r1", both(*[write(name, f"G = {number}\n") for number, name in enumerate(names)]))
    local(project, "r2", both(*[remove(name) for name in names], write("src/other.py", "OTHER = 1\n")))
    assert keep_changes("r2", project)["also_kept"] == ["r1"]
    sentence = f"{REPLACED}{tail}"
    assert_closed_as_replaced(project, "r1", "r2", names, sentence)


SNAPSHOT_KEYS = {
    "accept_requires_confirmation", "blocked_by", "can_discard", "can_keep", "cost_usd", "diff", "engine",
    "episode_id", "facts", "kept_with", "kind", "model_commentary", "outcome", "preview", "project_id", "repo_path",
    "request_id", "summary", "verification",
}
KEEP_KEYS = {"also_kept", "applied", "delta", "ok", "repo", "request_id"}
HISTORY_KEYS = {"complete", "created_ts", "episode_id", "outcome", "request", "request_id", "updated_ts"}
FACTS_KEYS = {"complete", "delta", "engine", "files", "model_id", "outcome", "pin", "provider"}


def test_ng5_every_reply_keeps_the_keys_the_app_reads_and_only_dropped_is_new(project):
    """The Mac app reads summary, can_keep, can_discard, blocked_by, outcome, facts.files, diff and cost_usd, and never
    kept_with, also_kept or a key it does not know, so it needs no change."""
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())

    def rpc(method: str, **params) -> dict:
        reply_ = server.dispatch({"id": "1", "method": method, "params": params})
        return reply_["result"] if reply_["ok"] else {"ERROR": reply_["error"]}

    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    path = str(project)
    shapes = {
        "open": rpc("snapshot", episode_id="r2", path=path),
        "held back": rpc("snapshot", episode_id="r1", path=path),
    }
    kept = rpc("keep", episode_id="r2", path=path)
    shapes["kept"] = rpc("snapshot", episode_id="r2", path=path)
    shapes["replaced"] = rpc("snapshot", episode_id="r1", path=path)
    for name, shape in shapes.items():
        assert SNAPSHOT_KEYS <= set(shape) <= SNAPSHOT_KEYS | {"dropped"}, name
    assert set(shapes["replaced"]["facts"]) == FACTS_KEYS
    assert set(kept) == KEEP_KEYS
    assert set(rpc("history", path=path)["episodes"][0]) == HISTORY_KEYS
    refused = rpc("keep", episode_id="r2", path=path)
    assert set(refused["ERROR"]) == {"code", "detail"} and refused["ERROR"]["code"] == "kept"


def test_the_snapshot_lists_what_was_dropped_and_outcome_says_replaced(project):
    """`dropped` is a sorted list on every snapshot but a discarded one's: [] unless a later Keep took files back."""
    local(project, "r1", both(write("src/gen.py", "GEN = 1\n"), write("src/aaa.py", "AAA = 1\n")))
    local(project, "r2", both(remove("src/gen.py"), remove("src/aaa.py"), write("src/other.py", "OTHER = 1\n")))
    assert snapshot("r1", repo=project)["dropped"] == [] and snapshot("r2", repo=project)["dropped"] == []
    keep_changes("r2", project)
    replaced_card = snapshot("r1", repo=project)
    assert replaced_card["dropped"] == ["src/aaa.py", "src/gen.py"], "sorted"
    assert replaced_card["outcome"] == "replaced" and replaced_card["facts"]["outcome"] == "replaced"
    assert snapshot("r2", repo=project)["dropped"] == [] and snapshot("r2", repo=project)["outcome"] == "kept"


# === Check 7: what worked stays as it is ==========================================================================


def test_ng1a_when_every_file_went_in_the_words_stay_as_they_are(project):
    """R2 documents the calc.py that R1 fixed."""
    local(project, "r1", calc_fix)
    local(project, "r2", calc_append)
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_went_in(project, "r1", "r2")
    assert history(project) == [("r2", "kept"), ("r1", "kept")]
    assert load_meta(project_id_for(project), "r1").get("dropped") is None, "nothing dropped, nothing recorded"


def test_ng1b_a_new_file_next_to_a_new_file_went_in(project):
    local(project, "r1", write("src/a.py", "A = 1\n"))
    local(project, "r2", write("src/b.py", "B = 1\n"))
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/a.py", "src/b.py"] and kept["also_kept"] == ["r1"]
    assert_closed_as_went_in(project, "r1", "r2")


def test_ng3_a_full_undo_leaves_the_older_request_keepable_for_the_next_cut(project):
    """R2 only removes R1's gen.py: R2 has no files, R1 is open and keepable (`undo-closes` changes this, not this)."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"))
    follow_up = card(project, "r2")
    assert follow_up["files"] == [] and follow_up["can_keep"] is False and follow_up["can_discard"] is False
    older = card(project, "r1")
    assert older["can_keep"] is True and older["blocked_by"] is None and older["outcome"] == "review"
    assert keep_changes("r1", project)["applied"] == ["src/gen.py"]


def test_ng4_a_continuation_of_a_discarded_request_does_not_turn_it_into_a_kept_one(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    assert discard_changes("r1", repo=project)["discarded"] is True
    local(project, "r2", write("src/other.py", "OTHER = 1\n"), continue_from="r1")
    assert load_meta(project_id_for(project), "r2")["built_on"] == "r1"
    assert keep_changes("r2", project)["also_kept"] == []
    assert card(project, "r1")["outcome"] == "discarded"
    assert history(project) == [("r2", "kept"), ("r1", "discarded")]


# === Named limit (a later cut, `closed-words`): the words are per file ===========================================


def test_the_words_are_per_file_so_a_file_that_was_changed_again_still_went_in(project):
    """Named limit (a later cut, `closed-words`): "went in" means the Keep wrote each of the request's files, not that
    every line of them is still there. R1 makes add() return a + b; R2 makes it return b + a in the same file: R1 reads
    "went in"."""
    local(project, "r1", calc_fix)
    local(project, "r2", lambda cwd: (cwd / "src" / "calc.py").write_text(
        (cwd / "src" / "calc.py").read_text(encoding="utf-8").replace(FIXED, "return b + a"), encoding="utf-8"))
    keep_changes("r2", project)
    assert_closed_as_went_in(project, "r1", "r2")
    assert "return a + b" not in (project / "src" / "calc.py").read_text(encoding="utf-8")


def test_the_words_are_per_file_for_an_undo_plus_an_edit_and_for_a_delete_then_recreate(project):
    """The other two corners of the same named limit."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", both(calc_bug, calc_append, remove("src/gen.py")))
    assert keep_changes("r2", project)["also_kept"] == ["r1"]
    assert card(project, "r1")["dropped"] == ["src/gen.py"], "calc.py is in R2's files, so it is not listed"
    other = project.parent / "other"
    shutil.copytree(FIXTURE, other)
    local(other, "q1", remove("src/__init__.py"))
    local(other, "q2", both(write("src/__init__.py", "# new\n"), write("src/other.py", "OTHER = 1\n")))
    assert keep_changes("q2", other)["also_kept"] == ["q1"]
    assert_closed_as_went_in(other, "q1", "q2")


# === NT13: the walk stops at a request with no files ===============================================================


def stack_with_an_idle_request_between(project: Path) -> None:
    """R1 adds gen.py; R2 (built on R1) changes nothing; R3 continues R2 and adds other.py."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", nothing)
    local(project, "r3", write("src/other.py", "OTHER = 1\n"), continue_from="r2")


def test_nt13_keep_stops_at_a_request_with_no_files(project):
    stack_with_an_idle_request_between(project)
    pid = project_id_for(project)
    assert [load_meta(pid, rid).get("built_on") for rid in ("r2", "r3")] == ["r1", "r2"]
    assert card(project, "r3")["files"] == ["src/other.py"], "R2 published its starting copy, so R3 lacks gen.py"
    kept = keep_changes("r3", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == [], "neither R2 nor R1 is carried"
    r1, r2 = snapshot("r1", repo=project), snapshot("r2", repo=project)
    assert r1["outcome"] == "review" and r1["can_keep"] is True and r1["can_discard"] is True
    assert r1["dropped"] == [] and r1["kept_with"] is None, "open, so nothing is dropped and nothing carried it"
    assert r2["outcome"] == "review" and r2["facts"]["files"] == [] and r2["can_keep"] is False
    assert r2["dropped"] == [] and r2["kept_with"] is None, "and R2 is not flipped to kept"
    assert history(project) == [("r3", "kept"), ("r2", "review"), ("r1", "review")]
    assert find_waiting_result(project).request_id == "r1", "R1 is still a result that waits"
    assert keep_changes("r1", project)["applied"] == ["src/gen.py"], "and it can still be kept"


# === `dropped`, one request at a time =============================================================================


INIT_TEXT = (FIXTURE / "src" / "__init__.py").read_text(encoding="utf-8")
DROPPED_KINDS = {
    # kind: (what R1 does, what R2 does to undo exactly that, the files R1 lists that the Keep of R2 did not write)
    "added": (write("src/gen.py", "GEN = 1\n"), remove("src/gen.py"), ["src/gen.py"]),
    "changed": (calc_fix, calc_bug, ["src/calc.py"]),
    "deleted": (remove("src/__init__.py"), write("src/__init__.py", INIT_TEXT), ["src/__init__.py"]),
    "renamed": (move("src/calc.py", "src/calculator.py"), move("src/calculator.py", "src/calc.py"),
                ["src/calc.py", "src/calculator.py"]),
}


@pytest.mark.parametrize("kind", sorted(DROPPED_KINDS))
def test_dropped_is_the_requests_files_minus_the_ones_the_keep_wrote(project, kind):
    """R1 does something to a file and R2 undoes exactly that, plus an unrelated new file: R2's own changes do not list
    the file, so its Keep does not write it and R1's `dropped` does."""
    first, second, dropped = DROPPED_KINDS[kind]
    local(project, "r1", first)
    local(project, "r2", both(second, write("src/other.py", "OTHER = 1\n")))
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == ["r1"], kind
    meta = load_meta(project_id_for(project), "r1")
    assert meta["kept"] is True and meta["kept_with"] == "r2" and meta["dropped"] == dropped, kind
    assert card(project, "r1")["dropped"] == dropped and card(project, "r1")["outcome"] == "replaced"


def test_dropped_is_not_recorded_and_not_listed_when_every_file_went_in(project):
    local(project, "r1", write("src/a.py", "A = 1\n"))
    local(project, "r2", write("src/b.py", "B = 1\n"))
    keep_changes("r2", project)
    assert "dropped" not in load_meta(project_id_for(project), "r1")
    assert card(project, "r1")["dropped"] == [] and card(project, "r2")["dropped"] == []


def test_dropped_of_a_request_in_a_chain_is_decided_against_what_the_keep_wrote(project):
    """R2's changes include R1's, so R2 and R1 each lose gen.py; R1 also changed calc.py, which R3 wrote."""
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    local(project, "r3", remove("src/gen.py"))
    keep_changes("r3", project)
    pid = project_id_for(project)
    assert load_meta(pid, "r2")["dropped"] == ["src/gen.py"] and load_meta(pid, "r1")["dropped"] == ["src/gen.py"]


def keep_r2_while_r1_cannot_be_saved(project: Path, monkeypatch) -> dict:
    """R2 removes the gen.py R1 added; Keep R2 while the disk refuses to save R1's record."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    real = coordinator._save_meta

    def save(meta: dict) -> None:
        if meta["request_id"] == "r1":
            raise OSError("disk full")
        real(meta)

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "_save_meta", save)
        return keep_changes("r2", project)


def test_a_record_that_cannot_be_saved_stays_as_it_was_and_the_keep_still_succeeds(project, monkeypatch):
    kept = keep_r2_while_r1_cannot_be_saved(project, monkeypatch)
    assert kept["ok"] is True and kept["applied"] == ["src/other.py"] and kept["also_kept"] == []
    assert "kept" not in load_meta(project_id_for(project), "r1") and "dropped" not in load_meta(
        project_id_for(project), "r1")
    assert load_meta(project_id_for(project), "r2")["kept"] is True


# === Old records: a request the old rule left open reads as closed =================================================
# An older build (or a record that could not be saved) left a request open although a kept request was built on it.


def reopen(project: Path, request_id: str) -> bytes:
    """Put a request's record back to what an old build left: neither kept nor discarded. Returns its bytes."""
    path = request_dir(project_id_for(project), request_id) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    for key in ("kept", "kept_with", "dropped"):
        meta.pop(key, None)
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def test_nt10_a_record_the_old_rule_left_open_reads_as_closed_and_is_never_rewritten(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    keep_changes("r2", project)
    record = reopen(project, "r1")
    path = request_dir(project_id_for(project), "r1") / "meta.json"
    assert "kept" not in json.loads(record)
    before = state(project)
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]
    assert nothing_waits(project)
    assert path.read_bytes() == record, "the reads and the refusals rewrote nothing"
    assert state(project) == before and not (project / "src" / "gen.py").exists()


def test_an_old_record_whose_files_all_went_in_reads_with_the_old_words(project):
    local(project, "r1", calc_fix)
    local(project, "r2", calc_append)
    keep_changes("r2", project)
    record = reopen(project, "r1")
    assert_closed_as_went_in(project, "r1", "r2")
    assert history(project) == [("r2", "kept"), ("r1", "kept")]
    assert (request_dir(project_id_for(project), "r1") / "meta.json").read_bytes() == record


def test_an_old_record_is_left_as_it_is_when_the_kept_requests_result_cannot_be_read(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    keep_changes("r2", project)
    reopen(project, "r1")
    shutil.rmtree(request_dir(project_id_for(project), "r2") / "candidate")
    seen = card(project, "r1")
    assert seen["outcome"] == "review" and seen["kept_with"] is None, "as before this cut: the safe direction"


def test_the_words_are_per_file_for_an_old_record_too(project):
    """The same named limit, read from the kept request's record instead of written by the Keep."""
    local(project, "r1", calc_fix)
    local(project, "r2", lambda cwd: (cwd / "src" / "calc.py").write_text(
        (cwd / "src" / "calc.py").read_text(encoding="utf-8").replace(FIXED, "return b + a"), encoding="utf-8"))
    keep_changes("r2", project)
    reopen(project, "r1")
    assert_closed_as_went_in(project, "r1", "r2")


def test_nt13_the_old_record_read_stops_there_too(project):
    """R1 stays open behind the idle R2 and reads open, while a request an ordinary stack carried reads as closed."""
    stack_with_an_idle_request_between(project)
    keep_changes("r3", project)
    local(project, "r4", write("src/a.py", "A = 1\n"))
    local(project, "r5", write("src/b.py", "B = 1\n"))
    assert load_meta(project_id_for(project), "r4").get("built_on") is None, "a new stack starts from the folder"
    assert keep_changes("r5", project)["also_kept"] == ["r4"]
    reopen(project, "r4")
    r1_record = (request_dir(project_id_for(project), "r1") / "meta.json").read_bytes()
    assert_closed_as_went_in(project, "r4", "r5")
    seen = card(project, "r1")
    assert seen["outcome"] == "review" and seen["can_keep"] is True and seen["kept_with"] is None
    assert history(project) == [
        ("r5", "kept"), ("r4", "kept"), ("r3", "kept"), ("r2", "review"), ("r1", "review"),
    ]
    assert (request_dir(project_id_for(project), "r1") / "meta.json").read_bytes() == r1_record


def test_a_record_that_could_not_be_saved_reads_as_closed_all_the_same(project, monkeypatch):
    keep_r2_while_r1_cannot_be_saved(project, monkeypatch)
    assert "kept" not in load_meta(project_id_for(project), "r1")
    assert_closed_as_replaced(project, "r1", "r2", ["src/gen.py"], GEN)
    assert history(project) == [("r2", "kept"), ("r1", "replaced")]


# === A result that lists no files has no files to name (QA x1, x2) ================================================

LISTS_NO_FILES = [
    "emptied",
    pytest.param("unreadable", marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads any folder")),
]


def lose_the_result(project: Path, request_id: str, how: str):
    """Empty a request's candidate/ folder, or take its mode away: it stays and lists no files. Returns the undo."""
    folder = request_dir(project_id_for(project), request_id) / "candidate"
    if how == "emptied":
        for child in list(folder.iterdir()):
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        return lambda: None
    folder.chmod(0)
    return lambda: folder.chmod(0o755)


@pytest.mark.parametrize("how", LISTS_NO_FILES)
def test_x1_x2_a_result_that_lists_no_files_is_closed_as_carried_not_as_replaced(project, how):
    """R1 adds gen.py, R2 (built on R1) adds other.py, then R1's candidate/ is emptied (x1) or made unreadable (x2).
    R2's Keep still writes gen.py: R2's own result holds it. R1 used to read "replaced ... Nothing from them went into
    conftest.py, local-coder.toml, src/__init__.py or 3 more files": a folder that lists nothing was read as a request
    that deleted all six files of its baseline. Its files cannot be told, so it is closed with the plain words."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", write("src/other.py", "OTHER = 1\n"))
    restore = lose_the_result(project, "r1", how)
    try:
        kept = keep_changes("r2", project)
        assert kept["applied"] == ["src/gen.py", "src/other.py"] and kept["also_kept"] == ["r1"]
        assert (project / "src" / "gen.py").exists(), "R1's file did land, with R2"
        after_keep = state(project)
        assert_closed_as_went_in(project, "r1", "r2")
        assert state(project) == after_keep, "the refused Keep and Discard of R1 write nothing"
        assert "dropped" not in load_meta(project_id_for(project), "r1")
        assert history(project) == [("r2", "kept"), ("r1", "kept")]
        assert nothing_waits(project)
    finally:
        restore()


def test_an_old_record_whose_result_lists_no_files_reads_as_went_in_too(project):
    """The same, read from the kept request's record for a request an older build left open (nothing is rewritten)."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", write("src/other.py", "OTHER = 1\n"))
    keep_changes("r2", project)
    record = reopen(project, "r1")
    lose_the_result(project, "r1", "emptied")
    assert_closed_as_went_in(project, "r1", "r2")
    assert history(project) == [("r2", "kept"), ("r1", "kept")]
    assert (request_dir(project_id_for(project), "r1") / "meta.json").read_bytes() == record
