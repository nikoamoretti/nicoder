"""Cut undo-closes: a follow-up that undoes the waiting result closes it for good, with a true sentence.

A request built on a result that still waits can take every change of that stack back out: R1 adds gen.py, R2 removes it
again. R2 then has no files of its own, yet R1 kept saying "I changed 1 file(s). Review the preview, then Keep or
Discard." with both buttons, a question was told it waits, and its Keep put back the file he had asked to remove (the
same for a fix that was put back, for a chain of three, and for the hosted agent's real `bash` `rm`).

Now a request that ends NORMALLY, built on a waiting result, with no files of its own because its working copy is
exactly the start of the stack (not the result it was built on), closes every waiting request it was built on, nearest
first, as `discarded` with `undone_by` naming it. The card, Keep (code `discarded`) and Discard (ok) say "A later
request undid these changes, so they were not added to your project." A run that was stopped, failed or cut short
closes nothing (Stop is "not that", a dropped stream is not a decision); neither does an idle follow-up (its copy is the
older result), a partial undo, or a request with a file of its own. A request he discarded himself keeps his words. Only
meta.json is written: never the folder, never the working copy.

Every test is modelled on a scenario of the cut's probe (UC the gaps, UG the guards, UR the named limits) and names the
check it belongs to. Fake engines and a scripted hosted agent ($0); the workspace, baselines, Keep, Discard, History and
the helper's RPC dispatch are the real code. Every refusal compares the folder before and after, file modes included.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, discard_changes, keep_changes, snapshot
from local_coder.general import coordinator, request_state
from local_coder.general.coordinator import (
    GeneralError,
    list_requests,
    live_dir,
    load_meta,
    project_id_for,
    request_dir,
    run_general_request,
)
from local_coder.general.snapshot import index_tree
from local_coder.general.waiting import find_waiting_result, waiting_note

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo_calc" / "repo"
BUG = "return a - b  # BUG: subtracts instead of adding"
FIXED = "return a + b"
HIS_CALC = "def add(a, b):\n    return 0  # Nico's own version\n"
NICO_NOTES = "Nico's notes\n"
UNDONE = "A later request undid these changes, so they were not added to your project."
DISCARDED_CARD = "Discarded. These changes were not added to your project."
KEEP_OF_DISCARDED = "This request was discarded, so there is nothing to keep."
ALREADY_DISCARDED = "Already discarded."
WENT_IN = "These changes went in with a later request you kept."

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
    """A copy of the demo calculator: `add` subtracts, three tests."""
    root = home / "projects" / "calc"
    shutil.copytree(FIXTURE, root)
    return root


# --- Engines: what a request does to its working copy -----------------------------------------------------------


def local(project: Path, request_id: str, engine, **kwargs) -> dict:
    """A request on the local path with a fake engine (Keep and the closing are one code path for every engine)."""
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd), **kwargs,
    )


def local_then_stop(project: Path, request_id: str, engine) -> dict:
    """A local request whose engine does its work and then Stop is pressed."""

    def stopping(cwd: Path, _text: str, cancel: threading.Event) -> None:
        engine(cwd)
        cancel.set()

    return run_general_request(project, f"request {request_id}", request_id=request_id, start_preview_after=False,
                               fake_engine=stopping, cancel_event=threading.Event())


def write(path: str, body: str):
    def engine(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def remove(path: str):
    return lambda cwd: (cwd / path).unlink()


def both(*engines):
    def engine(cwd: Path) -> None:
        for step in engines:
            step(cwd)

    return engine


def nothing(_cwd: Path) -> None:
    """An engine that changes nothing: the request ends with the copy exactly as the result it was built on."""


def calc_fix(cwd: Path) -> None:
    calc = cwd / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace(BUG, FIXED), encoding="utf-8")


def calc_bug(cwd: Path) -> None:
    calc = cwd / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace(FIXED, BUG), encoding="utf-8")


# --- A scripted hosted agent (the model is scripted; the tools, workspace and Keep are real) --------------------


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A scripted hosted model: each step is a reply, an error to raise, or a function that returns a reply."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)

    def __call__(self, _messages: list[dict], _tools: list[dict]) -> dict:
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step() if callable(step) else step


def edits(*operations: tuple) -> list[dict]:
    """The tool calls of an agent that makes these edits: ("write", path, text) or ("edit", path, old, new)."""
    calls = []
    for number, step in enumerate(operations):
        if step[0] == "write":
            calls.append(call("write", {"path": step[1], "content": step[2]}, f"c{number}"))
        else:
            calls.append(call("edit", {"path": step[1], "old_string": step[2], "new_string": step[3]}, f"c{number}"))
    return calls


def hosted_script(project: Path, request_id: str, script: Script, **kwargs) -> dict:
    return run_general_request(project, f"request {request_id}", request_id=request_id, agent_chat=script,
                               start_preview_after=False, **kwargs)


def hosted(project: Path, request_id: str, *operations: tuple, **kwargs) -> dict:
    """A request on the hosted agent whose scripted model makes these edits through the real tools, then answers."""
    return hosted_script(project, request_id, Script(reply(calls=edits(*operations)), reply("Done.")), **kwargs)


def bash(project: Path, request_id: str, *steps: dict, **kwargs) -> dict:
    """A hosted request whose agent runs its tool calls in the real sandbox (the only way it deletes a file)."""
    return hosted_script(project, request_id, Script(*steps, reply("Done.")), **kwargs)


def bash_call(command: str, call_id: str = "sh") -> dict:
    return reply(calls=[call("bash", {"command": command, "description": "run it"}, call_id)])


# --- What the scenarios look at ----------------------------------------------------------------------------------


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
        "blocked_by": review.get("blocked_by"), "files": review["facts"]["files"], "summary": review["summary"],
    }


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def record(project: Path, request_id: str) -> dict:
    return load_meta(project_id_for(project), request_id)


def refusal(action) -> tuple[str, str]:
    """(code, sentence) an action was refused with."""
    with pytest.raises(GeneralError) as raised:
        action()
    return raised.value.code, raised.value.detail


def keep_refusal(project: Path, request_id: str) -> tuple[str, str]:
    return refusal(lambda: keep_changes(request_id, project))


def nothing_waits(project: Path) -> bool:
    return find_waiting_result(project) is None and waiting_note(project, can_read=True) == ""


def calc_text(project: Path) -> str:
    return (project / "src" / "calc.py").read_text(encoding="utf-8")


def assert_undone(project: Path, request_id: str, by: str) -> None:
    """The request is closed because a later request undid it: nothing to press, the true words, final, and neither
    Keep nor Discard writes anything."""
    seen = card(project, request_id)
    assert seen["can_keep"] is False and seen["can_discard"] is False and seen["blocked_by"] is None
    assert seen["outcome"] == "discarded" and seen["files"] == [] and seen["summary"] == UNDONE
    meta = record(project, request_id)
    assert meta.get("discarded") is True and meta.get("undone_by") == by and not meta.get("kept")
    before = state(project)
    assert keep_refusal(project, request_id) == ("discarded", UNDONE)
    answer = discard_changes(request_id, repo=project)
    assert answer["ok"] is True and answer["discarded"] is True and answer["detail"] == UNDONE
    assert state(project) == before, "neither Keep nor Discard writes anything"


def assert_discarded_by_him(project: Path, request_id: str) -> None:
    """A request he discarded himself keeps his words, and has no `undone_by`."""
    seen = card(project, request_id)
    assert seen["summary"] == DISCARDED_CARD and seen["outcome"] == "discarded"
    assert "undone_by" not in record(project, request_id)
    assert keep_refusal(project, request_id) == ("discarded", KEEP_OF_DISCARDED)
    assert discard_changes(request_id, repo=project)["detail"] == ALREADY_DISCARDED


def assert_still_open(project: Path, request_id: str, files: list[str]) -> None:
    """A request still waiting: Keep and Discard offered, not held back, History `review`, its record untouched."""
    seen = card(project, request_id)
    assert seen["can_keep"] is True and seen["can_discard"] is True and seen["blocked_by"] is None
    assert seen["outcome"] == "review" and seen["files"] == files
    meta = record(project, request_id)
    assert not meta.get("discarded") and not meta.get("kept") and "undone_by" not in meta


# === Check 1: a full undo closes the older request, with the true words (UC1, UC2, UC3, UC8) ====================


def test_uc1_removing_the_file_the_older_request_added_closes_it(project):
    folder = state(project)
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"))
    follow_up = card(project, "r2")
    assert follow_up["files"] == [] and follow_up["can_keep"] is False and follow_up["can_discard"] is False
    assert follow_up["outcome"] == "review", "the follow-up keeps its own word"
    assert record(project, "r2")["built_on"] == "r1"
    assert_undone(project, "r1", "r2")
    assert history(project) == [("r2", "review"), ("r1", "discarded")]
    assert nothing_waits(project), "a question is not told that r1 waits"
    assert state(project) == folder and not (project / "src" / "gen.py").exists()


def test_uc2_putting_the_bug_back_closes_the_request_that_fixed_it(project):
    folder = state(project)
    local(project, "r1", calc_fix)
    local(project, "r2", calc_bug)
    assert card(project, "r2")["files"] == []
    assert_undone(project, "r1", "r2")
    assert nothing_waits(project)
    assert state(project) == folder and BUG in calc_text(project), "the fix never landed"


def test_uc3_the_same_through_the_hosted_agent(project):
    folder = state(project)
    hosted(project, "r1", ("edit", "src/calc.py", BUG, FIXED))
    hosted(project, "r2", ("edit", "src/calc.py", FIXED, BUG))
    meta = record(project, "r2")
    assert meta["engine"] == "agent" and meta["agent"]["status"] == "answered" and card(project, "r2")["files"] == []
    assert_undone(project, "r1", "r2")
    assert nothing_waits(project)
    assert state(project) == folder


def test_uc8_a_continuation_that_undoes_everything_closes_the_request_it_continues(project):
    """`continue_from` brings its own copy; the Mac app never sends it, but the rule holds for it too."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"), continue_from="r1")
    assert record(project, "r2")["built_on"] == "r1" and card(project, "r2")["files"] == []
    assert_undone(project, "r1", "r2")


# === Check 2: a chain is closed all the way; what he discarded stays his (UC4, UC7) =============================


def test_uc4_a_chain_of_three_is_closed_all_the_way_down(project):
    folder = state(project)
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    local(project, "r3", both(calc_bug, remove("src/gen.py"), remove("src/extra.py")))
    assert record(project, "r3")["built_on"] == "r2" and card(project, "r3")["files"] == []
    assert_undone(project, "r2", "r3")
    assert_undone(project, "r1", "r3")
    assert history(project) == [("r3", "review"), ("r2", "discarded"), ("r1", "discarded")]
    assert nothing_waits(project)
    assert state(project) == folder, "nothing of the stack is in the folder"


def test_uc7_a_request_he_discarded_stays_discarded_with_his_words(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    assert discard_changes("r2", repo=project)["discarded"] is True
    local(project, "r3", remove("src/gen.py"))
    assert record(project, "r3")["built_on"] == "r1", "r2 was passed over"
    assert_undone(project, "r1", "r3")
    assert_discarded_by_him(project, "r2")
    assert history(project) == [("r3", "review"), ("r2", "discarded"), ("r1", "discarded")]


# === Check 3: the agent's real `bash` delete (bash A, B) ========================================================

GEN_BY_BASH = "printf 'GEN = 1\\n' > src/gen.py && ls src"


@darwin_only
def test_bash_a_a_chain_of_three_through_the_agents_real_bash(project):
    """R1 (bash creates gen.py; edit fixes calc.py), he writes notes.txt, R2 (bash ls; write extra.py), R3 (bash `rm`
    of both files; edit puts the bug back): R3 ends with the copy exactly as the stack started."""
    folder = state(project)
    bash(project, "r1", bash_call(GEN_BY_BASH),
         reply(calls=edits(("edit", "src/calc.py", BUG, FIXED))))
    (project / "notes.txt").write_text(NICO_NOTES, encoding="utf-8")
    his = state(project)
    bash(project, "r2", bash_call("ls src", "ls"), reply(calls=edits(("write", "src/extra.py", "EXTRA = 1\n"))))
    bash(project, "r3", bash_call("rm src/gen.py src/extra.py && ls src", "rm"),
         reply(calls=edits(("edit", "src/calc.py", FIXED, BUG))))
    meta = record(project, "r3")
    assert meta["built_on"] == "r2" and meta["agent"]["status"] == "answered" and card(project, "r3")["files"] == []
    assert state(project) == his, "the folder is byte-identical after the run, his notes included"
    assert_undone(project, "r2", "r3")
    assert_undone(project, "r1", "r3")
    assert history(project) == [("r3", "review"), ("r2", "discarded"), ("r1", "discarded")]
    assert state(project) == {**folder, "notes.txt": ("file", 0o644, NICO_NOTES.encode())}


@darwin_only
def test_bash_b_the_agents_rm_of_the_file_the_earlier_request_made_closes_it(project):
    folder = state(project)
    bash(project, "r1", bash_call(GEN_BY_BASH))
    bash(project, "r2", bash_call("rm src/gen.py && ls src", "rm"))
    assert card(project, "r2")["files"] == []
    assert_undone(project, "r1", "r2")
    assert history(project) == [("r2", "review"), ("r1", "discarded")]
    assert state(project) == folder and not (project / "src" / "gen.py").exists(), "gen.py never lands"


# === Check 4: his edits survive; the next request starts from the folder (UC5, UC6) =============================


def test_uc5_his_edits_survive_the_undo(project):
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    (project / "src" / "calc.py").write_text(HIS_CALC, encoding="utf-8")
    (project / "notes.txt").write_text(NICO_NOTES, encoding="utf-8")
    his = state(project)
    local(project, "r2", both(calc_bug, remove("src/gen.py")))
    assert state(project) == his, "the folder is byte-identical after the undo run, his edits included"
    assert_undone(project, "r1", "r2")
    assert state(project) == his and not (project / "src" / "gen.py").exists()


def test_uc6_the_next_request_starts_from_the_folder_and_brings_nothing_back(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"))
    local(project, "r3", write("src/other.py", "OTHER = 1\n"))
    assert record(project, "r3").get("built_on") is None, "built on nothing: the stack is closed"
    assert card(project, "r3")["files"] == ["src/other.py"]
    kept = keep_changes("r3", project)
    assert kept["ok"] is True and kept["applied"] == ["src/other.py"] and kept["also_kept"] == []
    assert_undone(project, "r1", "r2")
    assert history(project) == [("r3", "kept"), ("r2", "review"), ("r1", "discarded")]
    assert not (project / "src" / "gen.py").exists() and (project / "src" / "other.py").exists()


# === Check 5: a stopped, failed or cut-short undo closes nothing (UG1 to UG5; UR1 reported) =====================


def test_ug1_a_stopped_local_undo_closes_nothing(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local_then_stop(project, "r2", remove("src/gen.py"))
    assert record(project, "r2")["stop_reason"] == "cancelled" and card(project, "r2")["files"] == []
    assert_still_open(project, "r1", ["src/gen.py"])
    assert history(project) == [("r2", "review"), ("r1", "review")]
    kept = keep_changes("r1", project)
    assert kept["ok"] is True and kept["applied"] == ["src/gen.py"], "its result is intact, so its Keep still works"


def test_ug2_a_stopped_hosted_undo_closes_nothing(project):
    hosted(project, "r1", ("edit", "src/calc.py", BUG, FIXED))
    cancel = threading.Event()

    def press_stop() -> dict:
        cancel.set()
        return reply(calls=[call("read", {"path": "src/calc.py"}, "r")])

    script = Script(reply(calls=edits(("edit", "src/calc.py", FIXED, BUG))), press_stop)
    hosted_script(project, "r2", script, cancel_event=cancel)
    meta = record(project, "r2")
    assert meta["agent"]["status"] == "stopped" and meta["stop_reason"] == "cancelled"
    assert card(project, "r2")["files"] == []
    assert_still_open(project, "r1", ["src/calc.py"])
    assert history(project) == [("r2", "stopped"), ("r1", "review")]


def test_ug3_a_failed_hosted_undo_closes_nothing(project):
    hosted(project, "r1", ("edit", "src/calc.py", BUG, FIXED))
    script = Script(reply(calls=edits(("edit", "src/calc.py", FIXED, BUG))), RuntimeError("the provider dropped it"))
    hosted_script(project, "r2", script)
    assert record(project, "r2")["agent"]["status"] == "error" and card(project, "r2")["files"] == []
    assert_still_open(project, "r1", ["src/calc.py"])
    assert history(project) == [("r2", "error"), ("r1", "review")]


def test_ug4_a_hosted_undo_cut_short_by_the_step_limit_closes_nothing(project):
    hosted(project, "r1", ("edit", "src/calc.py", BUG, FIXED))
    script = Script(reply(calls=edits(("edit", "src/calc.py", FIXED, BUG))), reply("I ran out of steps."))
    hosted_script(project, "r2", script, generation_limit=1)
    assert record(project, "r2")["agent"]["status"] == "step_limit" and card(project, "r2")["files"] == []
    assert_still_open(project, "r1", ["src/calc.py"])
    assert history(project) == [("r2", "step_limit"), ("r1", "review")]


def test_ug5_a_local_engine_that_raises_after_the_undo_closes_nothing_and_the_run_raises_as_before(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))

    def undo_then_fall_over(cwd: Path) -> None:
        (cwd / "src" / "gen.py").unlink()
        raise RuntimeError("the engine fell over")

    with pytest.raises(RuntimeError, match="the engine fell over"):
        local(project, "r2", undo_then_fall_over)
    assert_still_open(project, "r1", ["src/gen.py"])


@darwin_only
def test_bash_c_a_stop_after_the_agents_rm_closes_nothing(project):
    bash(project, "r1", bash_call(GEN_BY_BASH))
    cancel = threading.Event()

    def press_stop() -> dict:
        cancel.set()
        return reply(calls=[call("read", {"path": "src/calc.py"}, "r")])

    hosted_script(project, "r2", Script(bash_call("rm src/gen.py && ls src", "rm"), press_stop), cancel_event=cancel)
    meta = record(project, "r2")
    assert meta["agent"]["status"] == "stopped" and meta["stop_reason"] == "cancelled"
    assert card(project, "r2")["files"] == []
    assert_still_open(project, "r1", ["src/gen.py"])
    assert history(project) == [("r2", "stopped"), ("r1", "review")]


def test_ur1_after_a_stopped_undo_the_next_request_is_built_on_nothing(project):
    """Named limit (a later cut, `undo-stopped`): the copy is at the start of the stack after a stopped undo, so the
    next request starts from the folder, and the waiting request is left keepable on its own."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local_then_stop(project, "r2", remove("src/gen.py"))
    local(project, "r3", write("src/other.py", "OTHER = 1\n"))
    assert record(project, "r3").get("built_on") is None
    assert_still_open(project, "r1", ["src/gen.py"])


# === Check 6: what is not an undo closes nothing (UG6 to UG11; UR2 reported) ====================================


def test_ug6_an_idle_follow_up_closes_nothing(project):
    """Its copy is the older result, not the start of the stack, so an empty list of files is not enough."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", nothing)
    hosted_script(project, "r3", Script(reply("I looked at it; nothing to change.")))
    hosted_script(project, "r4", Script(RuntimeError("the provider dropped the stream")))
    for request_id in ("r2", "r3", "r4"):
        follow_up = card(project, request_id)
        assert follow_up["files"] == [] and not follow_up["can_keep"] and not follow_up["can_discard"], request_id
    assert_still_open(project, "r1", ["src/gen.py"])
    waiting = find_waiting_result(project)
    assert waiting is not None and waiting.request_id == "r1", "a question is still told that r1 waits"
    kept = keep_changes("r1", project)
    assert kept["ok"] is True and kept["applied"] == ["src/gen.py"]


def test_ug7_a_follow_up_with_a_file_of_its_own_holds_the_older_request_back_and_does_not_close_it(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", both(remove("src/gen.py"), write("src/other.py", "OTHER = 1\n")))
    held = card(project, "r1")
    assert held["blocked_by"] == "r2" and held["outcome"] == "review"
    assert held["can_keep"] is False and held["can_discard"] is False
    meta = record(project, "r1")
    assert not meta.get("discarded") and "undone_by" not in meta
    kept = keep_changes("r2", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == ["r1"], "net-out closes it at Keep"


def test_ug8_a_partial_undo_closes_nothing(project):
    local(project, "r1", both(calc_fix, write("src/gen.py", "GEN = 1\n")))
    local(project, "r2", calc_bug)
    assert card(project, "r2")["files"] == ["src/gen.py"], "R2 still lists gen.py"
    held = card(project, "r1")
    assert held["blocked_by"] == "r2" and held["outcome"] == "review"
    meta = record(project, "r1")
    assert not meta.get("discarded") and "undone_by" not in meta


def test_ug9_a_request_he_discarded_himself_keeps_its_own_words(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    assert discard_changes("r1", repo=project)["detail"] == "Discarded. The original project was not changed."
    assert_discarded_by_him(project, "r1")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to any file")
def test_ug10_a_record_that_cannot_be_saved_does_not_fail_the_run(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    path = request_dir(project_id_for(project), "r1") / "meta.json"
    path.chmod(0o444)
    try:
        local(project, "r2", remove("src/gen.py"))  # must not raise
    finally:
        path.chmod(0o644)
    assert_still_open(project, "r1", ["src/gen.py"])


def test_ug11_a_continuation_of_a_request_he_discarded_does_not_stamp_it(project):
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    discard_changes("r1", repo=project)
    local(project, "r2", write("src/other.py", "OTHER = 1\n"), continue_from="r1")
    local(project, "r3", both(remove("src/gen.py"), remove("src/other.py")), continue_from="r2")
    assert_discarded_by_him(project, "r1")


def tiny_project(home: Path, files: dict[str, str]) -> Path:
    root = home / "projects" / "tiny"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("files", "stop_reason", "closed"),
    [
        pytest.param({"src/app.py": "print('hi')\n"}, "checks_passed", True, id="a_project_with_src"),
        pytest.param({"app.py": "print('hi')\n"}, "not_repaired", False, id="a_flat_project"),
    ],
)
def test_a_local_undo_closes_only_where_local_coders_own_checks_pass(home, files, stop_reason, closed):
    """Named limit: the local engine checks the finished copy, and a project without `src/` fails that check, so the run
    does not end `checks_passed` and counts as cut short: the older request stays keepable there, as it was before this
    cut. (A fake engine has no model to repair with and ends `not_repaired`; the real engine retries its repairs first
    and ends in another stop. Either way it is not `checks_passed`.)"""
    project = tiny_project(home, files)
    local(project, "r1", write("gen.py", "GEN = 1\n"))
    local(project, "r2", remove("gen.py"))
    assert record(project, "r2")["stop_reason"] == stop_reason
    if closed:
        assert_undone(project, "r1", "r2")
    else:
        assert_still_open(project, "r1", ["gen.py"])


def test_the_hosted_agent_closes_on_a_flat_project_too(home):
    """The agent skips Local Coder's checks when there are no files, so a flat project is no exception for it."""
    project = tiny_project(home, {"app.py": "print('hi')\n"})
    hosted(project, "r1", ("edit", "app.py", "print('hi')", "print('hello')"))
    hosted(project, "r2", ("edit", "app.py", "print('hello')", "print('hi')"))
    assert record(project, "r2")["agent"]["status"] == "answered" and card(project, "r2")["files"] == []
    assert_undone(project, "r1", "r2")


def test_ur2_a_record_an_older_build_left_open_is_not_re_read_as_closed(project):
    """Named limit: the records of an idle follow-up and of an undo are the same, so nothing is derived on read. A
    request an undo left open before this cut stays open (he can Discard it)."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"))
    path = request_dir(project_id_for(project), "r1") / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    for key in ("discarded", "undone_by"):
        meta.pop(key, None)
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    assert_still_open(project, "r1", ["src/gen.py"])


# === Check 7: the replies keep every key (UC9) ==================================================================


def rpc(method: str, project: Path, request_id: str | None = None) -> dict:
    params = {"path": str(project)} if request_id is None else {"episode_id": request_id, "path": str(project)}
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    return server.dispatch({"id": "1", "method": method, "params": params})


def test_uc9_an_undone_request_answers_with_the_keys_of_a_discarded_one(project, home):
    reference = home / "projects" / "reference"
    shutil.copytree(FIXTURE, reference)
    local(reference, "q1", write("src/self.py", "SELF = 1\n"))
    discard_changes("q1", repo=reference)  # a request he discarded himself: the reference shapes
    ref = {
        "snapshot": sorted(snapshot("q1", repo=reference)),
        "discard": sorted(discard_changes("q1", repo=reference)),
        "keep": sorted(rpc("keep", reference, "q1")["error"]),
        "history": sorted(list_requests(reference)[0]),
    }
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"))
    assert sorted(snapshot("r1", repo=project)) == ref["snapshot"]
    assert sorted(discard_changes("r1", repo=project)) == ref["discard"] == ["detail", "discarded", "ok", "request_id"]
    kept = rpc("keep", project, "r1")
    assert kept["ok"] is False and sorted(kept["error"]) == ref["keep"] == ["code", "detail"]
    assert kept["error"] == {"code": "discarded", "detail": UNDONE}
    assert sorted(next(row for row in list_requests(project) if row["request_id"] == "r1")) == ref["history"]


# === The step itself: what it reads, what it writes, and what it leaves alone ===================================


@pytest.fixture
def undone_stack(project, monkeypatch):
    """R1 adds gen.py and R2 (built on R1) removes it, with the closing step switched off while R2 runs: R1 is still
    open, exactly as an undo leaves it before the step. Returns the project, R2's record and the real step."""
    real = coordinator._close_what_was_undone
    monkeypatch.setattr(coordinator, "_close_what_was_undone", lambda _meta, _cancel: [])
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", remove("src/gen.py"))
    monkeypatch.setattr(coordinator, "_close_what_was_undone", real)
    assert_still_open(project, "r1", ["src/gen.py"])
    return project, record(project, "r2"), real


def test_a_normal_end_closes_what_was_undone_and_a_second_call_changes_nothing(undone_stack):
    project, meta, step = undone_stack
    assert step(meta, threading.Event()) == ["r1"]
    assert_undone(project, "r1", "r2")
    assert step(meta, threading.Event()) == [], "r1 is not waiting any more"


def test_the_step_writes_only_the_older_requests_record(undone_stack):
    project, meta, step = undone_stack
    pid = project_id_for(project)
    folder, live = state(project), index_tree(live_dir(pid) / "candidate").files.keys()
    own = (request_dir(pid, "r2") / "meta.json").read_bytes()
    r1_candidate = state(request_dir(pid, "r1") / "candidate")
    assert step(meta, threading.Event()) == ["r1"]
    assert state(project) == folder, "never the folder"
    assert index_tree(live_dir(pid) / "candidate").files.keys() == live, "never the working copy"
    assert (request_dir(pid, "r2") / "meta.json").read_bytes() == own, "never the undoing request's own record"
    assert state(request_dir(pid, "r1") / "candidate") == r1_candidate, "its evidence stays on disk"


STOP_MARKERS = {
    "stopped_incomplete": {"stopped_incomplete": True},
    "stop_reason_cancelled": {"stop_reason": "cancelled"},
    "stop_reason_not_repaired": {"stop_reason": "not_repaired"},
    "stop_reason_max_repairs": {"stop_reason": "max_repairs"},
    "agent_stopped": {"engine": "agent", "agent": {"status": "stopped"}},
    "agent_error": {"engine": "agent", "agent": {"status": "error"}},
    "agent_step_limit": {"engine": "agent", "agent": {"status": "step_limit"}},
    "agent_time_limit": {"engine": "agent", "agent": {"status": "time_limit"}},
    "agent_without_a_status": {"engine": "agent"},
}


@pytest.mark.parametrize("marker", sorted(STOP_MARKERS))
def test_each_stop_marker_alone_keeps_the_step_from_closing_anything(undone_stack, marker):
    project, meta, step = undone_stack
    assert step({**meta, **STOP_MARKERS[marker]}, threading.Event()) == []
    assert_still_open(project, "r1", ["src/gen.py"])
    assert step(meta, threading.Event()) == ["r1"], "the same record without the marker does close it"


def test_a_cancel_that_was_pressed_keeps_the_step_from_closing_anything(undone_stack):
    project, meta, step = undone_stack
    pressed = threading.Event()
    pressed.set()
    assert step(meta, pressed) == []
    assert_still_open(project, "r1", ["src/gen.py"])


@pytest.mark.parametrize(
    "meta_change",
    [{"stop_reason": "checks_passed"}, {"stop_reason": ""}, {"engine": "agent", "agent": {"status": "answered"}}],
    ids=["checks_passed", "empty_stop_reason", "agent_answered"],
)
def test_a_normal_end_in_each_of_its_shapes_closes(undone_stack, meta_change):
    project, meta, step = undone_stack
    assert step({**meta, **meta_change}, threading.Event()) == ["r1"]


def test_a_request_built_on_nothing_closes_nothing(undone_stack):
    project, meta, step = undone_stack
    assert step({key: value for key, value in meta.items() if key != "built_on"}, threading.Event()) == []
    assert_still_open(project, "r1", ["src/gen.py"])


def test_a_record_that_cannot_be_saved_is_left_as_it_was_and_the_step_does_not_raise(undone_stack, monkeypatch):
    project, meta, step = undone_stack

    def disk_full(_record: dict) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "_save_meta", disk_full)
        assert step(meta, threading.Event()) == []
    assert_still_open(project, "r1", ["src/gen.py"])


# --- A chain of three: what the walk does with a record it cannot save, a result it cannot read, a damaged record ---


@contextlib.contextmanager
def record_that_cannot_be_saved(project: Path, request_id: str, how: str, monkeypatch) -> Iterator[None]:
    """This request's record cannot be written for the length of the block: a read-only meta.json (as in QA's E2, not
    as root) or a save that raises (any user). Every other record saves as usual."""
    if how == "read_only_file":
        if os.geteuid() == 0:
            pytest.skip("root writes to any file")
        path = request_dir(project_id_for(project), request_id) / "meta.json"
        path.chmod(0o444)
        try:
            yield
        finally:
            path.chmod(0o644)
        return
    real = coordinator._save_meta

    def save(meta: dict) -> None:
        if meta.get("request_id") == request_id:
            raise OSError(13, "Permission denied", "meta.json")
        real(meta)

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "_save_meta", save)
        yield


@pytest.mark.parametrize("how", ["read_only_file", "save_raises"])
def test_m20_a_record_that_cannot_be_saved_ends_the_walk_and_no_older_request_is_closed(project, monkeypatch, how):
    """QA's E2. R1 adds gen.py, R2 adds extra.py, R3 removes both, and R2's record cannot be saved: R2 stays open and
    the walk ends there. Had it gone on, R1 would read "not added to your project" while a Keep of R2, still open,
    writes gen.py (it carries R1's result). The run does not fail."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    with record_that_cannot_be_saved(project, "r2", how, monkeypatch):
        local(project, "r3", both(remove("src/gen.py"), remove("src/extra.py")))  # must not raise
    assert record(project, "r3")["built_on"] == "r2" and card(project, "r3")["files"] == []
    assert_still_open(project, "r2", ["src/extra.py", "src/gen.py"])
    held = card(project, "r1")
    assert held["blocked_by"] == "r2" and held["outcome"] == "review", "R1 is open, held back by the open R2"
    meta = record(project, "r1")
    assert not meta.get("discarded") and not meta.get("kept") and "undone_by" not in meta
    assert history(project) == [("r3", "review"), ("r2", "review"), ("r1", "review")]
    kept = keep_changes("r2", project)  # what the open R2 still does: it carries R1 in, and R1 says so
    assert kept["ok"] is True and kept["applied"] == ["src/extra.py", "src/gen.py"] and kept["also_kept"] == ["r1"]
    assert card(project, "r1")["summary"] == WENT_IN and (project / "src" / "gen.py").exists()


def raises(error: type[Exception]):
    def damaged(_metas, _request_id):
        raise error("a damaged record")

    return damaged


@pytest.mark.parametrize("error", [AttributeError, KeyError, OSError, TypeError, ValueError], ids=lambda e: e.__name__)
def test_m21_a_record_or_result_that_cannot_be_read_ends_the_step_without_closing_or_raising(
    undone_stack, monkeypatch, error,
):
    """The walk reads every record and result of the stack. A damaged one (a field that does not parse, a missing key, a
    result that cannot be opened) leaves the older request as it was and never fails the step."""
    project, meta, step = undone_stack
    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "_carried_chain", raises(error))
        assert step(meta, threading.Event()) == []
    assert_still_open(project, "r1", ["src/gen.py"])
    assert step(meta, threading.Event()) == ["r1"], "the same call without the damage does close it"


def test_m21_a_damaged_record_met_at_the_end_of_a_run_does_not_fail_that_run(project, monkeypatch):
    """The step runs after the work is done and saved: a failure in it would turn a finished run into an error."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "_carried_chain", raises(ValueError))
        local(project, "r2", remove("src/gen.py"))  # must not raise
    own = card(project, "r2")
    assert own["files"] == [] and own["outcome"] == "review" and own["can_keep"] is False
    assert record(project, "r2")["built_on"] == "r1" and record(project, "r2")["stop_reason"] == "checks_passed"
    assert_still_open(project, "r1", ["src/gen.py"])
    assert history(project) == [("r2", "review"), ("r1", "review")]


def test_m26_a_result_that_cannot_be_read_is_left_as_it_was(project):
    """QA's E1. R1 adds gen.py, R2 adds extra.py, and R1's candidate/ is gone when R3 removes both. R2 is closed, but
    R1's result cannot be read, so its record is not touched: it would say "undone" about changes nobody can see."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    pid = project_id_for(project)
    shutil.rmtree(request_dir(pid, "r1") / "candidate")
    before = (request_dir(pid, "r1") / "meta.json").read_bytes()
    local(project, "r3", both(remove("src/gen.py"), remove("src/extra.py")))
    assert record(project, "r3")["built_on"] == "r2" and card(project, "r3")["files"] == []
    assert_undone(project, "r2", "r3")
    assert (request_dir(pid, "r1") / "meta.json").read_bytes() == before, "R1's record is not touched"
    meta = record(project, "r1")
    assert not meta.get("discarded") and not meta.get("kept") and "undone_by" not in meta


def test_a_request_in_the_middle_that_is_not_waiting_keeps_its_own_record(project, monkeypatch):
    """The walk goes through it (it still has files) and stamps only the requests that wait."""
    real = coordinator._close_what_was_undone
    monkeypatch.setattr(coordinator, "_close_what_was_undone", lambda _meta, _cancel: [])
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", write("src/extra.py", "EXTRA = 1\n"))
    local(project, "r3", both(remove("src/gen.py"), remove("src/extra.py")))
    monkeypatch.setattr(coordinator, "_close_what_was_undone", real)
    path = request_dir(project_id_for(project), "r2") / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**meta, "discarded": True}, indent=2) + "\n", encoding="utf-8")  # as if he had
    before = path.read_bytes()
    assert real(record(project, "r3"), threading.Event()) == ["r1"]
    assert path.read_bytes() == before, "his record is not touched"
    assert_undone(project, "r1", "r3")
    assert "undone_by" not in record(project, "r2")


def test_a_request_built_on_a_result_that_lists_no_files_still_stops_the_walk(project, monkeypatch):
    """R2 changed nothing (idle), R3 continues R2 and undoes its own work: the walk stops at R2, so R1 is not closed."""
    local(project, "r1", write("src/gen.py", "GEN = 1\n"))
    local(project, "r2", nothing)
    local(project, "r3", write("src/other.py", "OTHER = 1\n"), continue_from="r2")
    local(project, "r4", remove("src/other.py"), continue_from="r3")
    assert record(project, "r4")["built_on"] == "r3"
    assert_undone(project, "r3", "r4")
    assert_still_open(project, "r1", ["src/gen.py"])


def test_the_words_belong_to_a_discarded_record_that_names_its_undoer():
    assert request_state.was_undone({"discarded": True, "undone_by": "r2"}) is True
    assert request_state.was_undone({"discarded": True}) is False, "he discarded it himself"
    assert request_state.was_undone({"undone_by": "r2"}) is False, "not closed"
    assert request_state.was_undone({"kept": True, "discarded": True, "undone_by": "r2"}) is False, "kept wins"
    assert request_state.was_undone(None) is False and request_state.was_undone("r1") is False
    assert request_state.UNDONE_SENTENCE == UNDONE and request_state.UNDONE_BY == "undone_by"
