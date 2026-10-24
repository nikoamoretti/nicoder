"""Cut stack-survives: the waiting result survives a Discard, and a follow-up that changes nothing holds nothing back.

Every engine builds a request on the result that still waits in the project's sticky working copy (`built_on`).
Two things used to cut that stack for no reason Nico could see:

* A Discard put the working copy back to the discarded request's own baseline, which is the start of the stack. The
  result it was built on was gone from the copy, so the retry (or the next request) started from the folder and the
  waiting result could only be kept apart from it, and then not at all.
* A follow-up that failed, was stopped or changed nothing still listed the older result's files as its own, offered
  Keep and Discard and held that result back; the next request was then built on it as on a real result.

Now a Discard puts the copy back to the result the request was built on while that still waits (a request with no
files of its own leaves the copy alone), a request that ends with the copy exactly as that result has no result of
its own, and the next request passes over discarded and such empty requests and builds on the newest one left.

A scripted hosted model (`agent_chat`, $0) and fake engines stand in for the models; the workspace, baselines,
tools, access rules, Keep, Discard and History are the real code. Every test is modelled on a scenario of the
cut's probe and names the acceptance check it belongs to.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, discard_changes, keep_changes, snapshot
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
DOCUMENTED = FIXED + "  # documented"
NICO_TEST_LINE = "# Nico's own edit while the result waits\n"
NICO_NOTES = "Nico's notes\n"
HIS_CALC = "def add(a, b):\n    return 0  # Nico's own version\n"
WENT_IN = "These changes went in with a later request you kept."
REPLACED_GEN = (
    "These changes were replaced by a later request you kept. Nothing from them went into src/gen.py."
)
NEWER_FIRST = "A newer request in this project builds on this one, so Keep or Discard the newer one first."
NEWER_RUNNING = (
    "A newer request in this project builds on this one and is still running, so let it finish or stop it first."
)
STILL_RUNNING = "A request is still running in this project. Wait for it to finish, or stop it, then discard."
STOPPED_NOTHING = "Stopped. Nothing was changed."
DISCARDED_NOTHING_TO_KEEP = "This request was discarded, so there is nothing to keep."
R1_TITLE = "Fix add so it adds instead of subtracting."


def conflict(what: str) -> str:
    """Nico's sentence for a refused Keep: which file, and what happened to it."""
    return f"Not kept: since these changes were started, {what}. Your project was not changed."


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME with the data dir inside it, so the agent may read a waiting copy as it can in the app."""
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


# --- A scripted hosted model, fake engines and the things the scenarios look at ---------------------------------


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A scripted hosted model. A step is a reply, an error to raise, or a function of what its last tool said.

    `hook` runs at the first model call (the request is still being made); `on_call(n)` runs before call number n.
    """

    def __init__(self, *steps, on_call=None) -> None:
        self.steps, self.sent, self.hook, self.on_call = list(steps), [], None, on_call

    def __call__(self, messages: list[dict], _tools: list[dict]) -> dict:
        if not self.sent and self.hook:
            self.hook()
        if self.on_call:
            self.on_call(len(self.sent))
        self.sent.append(json.loads(json.dumps(messages)))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(self.last_tool()) if callable(step) else step

    def last_tool(self) -> str:
        tools = [m for m in (self.sent[-1] if self.sent else []) if m.get("role") == "tool"]
        return str(tools[-1].get("content")) if tools else ""

    def result(self, call_id: str) -> str:
        """What the tool call with this id told the model, as it saw it at its last call."""
        for message in self.sent[-1] if self.sent else []:
            if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
                return str(message.get("content"))
        return ""


def run(project: Path, request_id: str, text: str, script, **kwargs) -> dict:
    return run_general_request(project, text, request_id=request_id, agent_chat=script,
                               start_preview_after=False, **kwargs)


def local(project: Path, request_id: str, engine) -> dict:
    """A request on the local path with a fake engine."""
    return run_general_request(project, f"request {request_id}", request_id=request_id, fake_engine=engine,
                               start_preview_after=False)


def fix(project: Path, request_id: str = "req-1") -> dict:
    """The first request: the agent reads calc.py and fixes add()."""
    script = Script(
        reply(calls=[call("read", {"path": "src/calc.py"}, "a")]),
        reply(calls=[call("edit", {"path": "src/calc.py", "old_string": BUG, "new_string": FIXED}, "b")]),
        reply("Fixed add()."),
    )
    return run(project, request_id, R1_TITLE, script)


def document(seen: str) -> dict:
    """A model edits what it saw: the fixed line when the waiting fix is there, the buggy line when it is not."""
    old = FIXED if FIXED in seen else BUG
    return reply(calls=[call("edit", {"path": "src/calc.py", "old_string": old, "new_string": old + "  # documented"},
                             "d")])


def documenting() -> Script:
    """A retry that reads calc.py (call id "a") and documents add()."""
    return Script(reply(calls=[call("read", {"path": "src/calc.py"}, "a")]), document, reply("Documented add()."))


def fails() -> Script:
    return Script(RuntimeError("the provider dropped the stream"))


def add_module(path: str, body: str, *more: dict) -> dict:
    return reply(calls=[call("write", {"path": path, "content": body}, "w"), *more])


def adds(path: str, body: str, text: str = "Added.") -> Script:
    return Script(add_module(path, body), reply(text))


def writes_file(path: str, body: str):
    def engine(cwd: Path, _text: str, _cancel) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(body, encoding="utf-8")

    return engine


def fixes_calc(cwd: Path, _text: str, _cancel) -> None:
    calc = cwd / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace(BUG, FIXED), encoding="utf-8")


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def identity(root: Path) -> dict[str, str]:
    """The source files of a tree with their content identity (what a snapshot sees)."""
    return {rel: entry.sha256 for rel, entry in index_tree(root).files.items()}


def working_copy(project: Path) -> Path:
    return live_dir(project_id_for(project)) / "candidate"


def record(project: Path, request_id: str) -> dict:
    """What the request's record says right now: its link and the files its baseline lists."""
    pid = project_id_for(project)
    frozen = json.loads((request_dir(pid, request_id) / "baseline.json").read_text(encoding="utf-8"))["files"]
    return {"built_on": load_meta(pid, request_id).get("built_on"), "baseline_files": sorted(frozen)}


def built_on(project: Path, request_id: str) -> str | None:
    return load_meta(project_id_for(project), request_id).get("built_on")


def baseline_of(project: Path, request_id: str) -> tuple[bytes, dict[str, bytes]]:
    root = request_dir(project_id_for(project), request_id)
    return (root / "baseline.json").read_bytes(), tree(root / "baseline")


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def refusal(action) -> tuple[str, str] | None:
    """(code, sentence) an action was refused with, or None when it went through."""
    try:
        action()
    except GeneralError as exc:
        return exc.code, exc.detail
    return None


def hand_edits(project: Path) -> None:
    """What Nico does in the folder while a result waits: edits a file and adds one."""
    test = project / "tests" / "test_calc.py"
    test.write_text(test.read_text(encoding="utf-8") + NICO_TEST_LINE, encoding="utf-8")
    (project / "notes.txt").write_text(NICO_NOTES, encoding="utf-8")


def hand_edits_survive(project: Path) -> bool:
    return ((project / "tests" / "test_calc.py").read_text(encoding="utf-8").endswith(NICO_TEST_LINE)
            and (project / "notes.txt").read_text(encoding="utf-8") == NICO_NOTES)


def add_line(project: Path) -> str:
    """The `return a ...` line of add() in the folder."""
    return next(line.strip() for line in (project / "src" / "calc.py").read_text(encoding="utf-8").splitlines()
                if "return a" in line and "*" not in line)


# --- Check 1: an error, a Discard, a retry; Keep takes R1 alone or with the retry -------------------------------


def test_an_error_then_a_discard_then_a_retry_builds_on_the_result_that_waits(project):
    """SS1, the flow: R1 waits and he edits his notes; R2's model fails; he discards R2 and sends it again as R3."""
    pid = project_id_for(project)
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also document add().", fails())
    assert snapshot("req-2", repo=project)["outcome"] == "error"
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    assert discard_changes("req-2", repo=project)["discarded"] is True
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder, "the Discard touches neither"
    retry = documenting()
    at_first_call: dict = {}
    retry.hook = lambda: at_first_call.update(record(project, "req-3"))
    review = run(project, "req-3", "Now also document add().", retry)
    assert at_first_call["built_on"] == "req-1", "built on R1 before the agent's first call"
    assert FIXED in retry.result("a") and "BUG" not in retry.result("a"), "its agent reads R1's fix, not the bug"
    assert baseline_of(project, "req-3") == baseline_of(project, "req-1"), "one starting point for the stack"
    assert review["facts"]["files"] == ["src/calc.py"], "R3 lists calc.py only, none of his edits"
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder, "nothing lands before Keep"
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["can_discard"] is False
    assert older["blocked_by"] == "req-3" and older["summary"] == NEWER_FIRST
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]
    assert add_line(project) == DOCUMENTED and hand_edits_survive(project)
    assert history(project) == [("req-3", "kept"), ("req-2", "discarded"), ("req-1", "kept")]
    assert waiting_note(project, can_read=True) == "", "a question afterwards is told nothing waits"
    assert refusal(lambda: keep_changes("req-1", project)) == ("kept", "These changes are already in your project.")


def test_the_older_request_can_be_kept_alone_after_the_follow_up_was_discarded(project):
    """SS1b: Discard R2 and R1 is free again; Keep R1 takes src/calc.py alone, his edits stay."""
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also document add().", fails())
    discard_changes("req-2", repo=project)
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["can_discard"] is True and older["blocked_by"] is None
    kept = keep_changes("req-1", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == []
    assert hand_edits_survive(project) and add_line(project) == FIXED
    assert history(project) == [("req-2", "discarded"), ("req-1", "kept")]


def test_the_older_request_can_be_kept_alone_after_the_retry_was_discarded_too(project):
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also document add().", fails())
    discard_changes("req-2", repo=project)
    run(project, "req-3", "Now also document add().", documenting())
    assert discard_changes("req-3", repo=project)["discarded"] is True
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None
    kept = keep_changes("req-1", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == []
    assert add_line(project) == FIXED, "the retry's documentation never landed"
    assert hand_edits_survive(project)
    assert history(project) == [("req-3", "discarded"), ("req-2", "discarded"), ("req-1", "kept")]


def test_a_clash_after_the_discard_is_refused_and_writes_nothing(project):
    """SS5: he edits src/calc.py after the retry. Keep R3 is refused, and so is Keep R1 once R3 is discarded."""
    fix(project)
    run(project, "req-2", "Now also document add().", fails())
    discard_changes("req-2", repo=project)
    run(project, "req-3", "Now also document add().", documenting())
    (project / "src" / "calc.py").write_text(HIS_CALC, encoding="utf-8")
    folder = tree(project)
    assert refusal(lambda: keep_changes("req-3", project)) == (
        "conflict", conflict("src/calc.py was changed in your project"),
    )
    assert tree(project) == folder, "a refused Keep writes nothing"
    still = snapshot("req-3", repo=project)
    assert still["can_keep"] is True and still["can_discard"] is True and still["outcome"] == "review"
    assert discard_changes("req-3", repo=project)["discarded"] is True
    assert snapshot("req-1", repo=project)["can_keep"] is True
    assert refusal(lambda: keep_changes("req-1", project)) == (
        "conflict", conflict("src/calc.py was changed in your project"),
    ), "he edited a file R1 changed"
    assert tree(project) == folder and (project / "src" / "calc.py").read_text(encoding="utf-8") == HIS_CALC
    assert refusal(lambda: keep_changes("req-2", project)) == ("discarded", DISCARDED_NOTHING_TO_KEEP)
    assert discard_changes("req-3", repo=project)["detail"] == "Already discarded.", "a Discard is final and repeatable"


# --- Check 2: other ways the top is discarded: stopped with a file, a stack of three, the local path -------------


def test_a_follow_up_stopped_after_writing_a_file_is_discarded_and_the_retry_builds_on_the_result(project):
    """SS2: R2 is stopped after writing src/extra.py. Discarding it takes the file out of the copy only."""
    pid = project_id_for(project)
    fix(project)
    hand_edits(project)
    cancel = threading.Event()
    second = Script(
        add_module("src/extra.py", "EXTRA = 1\n"), reply(calls=[call("read", {"path": "src/calc.py"}, "x")]),
        reply("never reached"), on_call=lambda n: cancel.set() if n == 1 else None,
    )
    run(project, "req-2", "Now also add an extra module.", second, cancel_event=cancel)
    stopped = snapshot("req-2", repo=project)
    assert stopped["outcome"] == "stopped" and "src/extra.py" in stopped["facts"]["files"], "a real result"
    assert (working_copy(project) / "src" / "extra.py").exists()
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    assert discard_changes("req-2", repo=project)["discarded"] is True
    assert not (working_copy(project) / "src" / "extra.py").exists(), "the Discard takes R2's file out of the copy"
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder, "and writes nothing else"
    third = Script(reply(calls=[call("list", {"path": "src"}, "l"), call("read", {"path": "src/calc.py"}, "a")]),
                   document, reply("Documented add()."))
    at_first_call: dict = {}
    third.hook = lambda: at_first_call.update(record(project, "req-3"))
    review = run(project, "req-3", "Now also document add().", third)
    assert at_first_call["built_on"] == "req-1"
    assert "extra.py" not in third.result("l") and FIXED in third.result("a")
    assert review["facts"]["files"] == ["src/calc.py"]
    assert not (request_dir(pid, "req-3") / "candidate" / "src" / "extra.py").exists()
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]
    assert not (project / "src" / "extra.py").exists() and hand_edits_survive(project)


def test_the_top_of_a_stack_of_three_is_discarded_and_the_next_request_builds_on_the_middle_one(project):
    """SS3: one Keep of the fourth request takes the three files and closes the second and the first."""
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also add an extra module.", adds("src/extra.py", "EXTRA = 1\n", "Added extra.py."))
    run(project, "req-3", "And a third one.", adds("src/third.py", "THIRD = 3\n", "Added third.py."))
    assert discard_changes("req-3", repo=project)["discarded"] is True
    middle = snapshot("req-2", repo=project)
    assert middle["can_keep"] is True and middle["blocked_by"] is None, "the middle request is free to be decided"
    fourth = Script(reply(calls=[call("list", {"path": "src"}, "l"), call("read", {"path": "src/calc.py"}, "a")]),
                    add_module("src/fourth.py", "FOURTH = 4\n"), reply("Added fourth.py."))
    at_first_call: dict = {}
    fourth.hook = lambda: at_first_call.update(record(project, "req-4"))
    review = run(project, "req-4", "And a different third one.", fourth)
    assert at_first_call["built_on"] == "req-2"
    assert "extra.py" in fourth.result("l") and "third.py" not in fourth.result("l") and FIXED in fourth.result("a")
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py", "src/fourth.py"]
    assert [snapshot(f"req-{n}", repo=project)["blocked_by"] for n in (1, 2)] == ["req-4", "req-4"]
    kept = keep_changes("req-4", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py", "src/fourth.py"]
    assert kept["also_kept"] == ["req-2", "req-1"]
    assert not (project / "src" / "third.py").exists() and hand_edits_survive(project)
    assert history(project) == [("req-4", "kept"), ("req-3", "discarded"), ("req-2", "kept"), ("req-1", "kept")]


def test_the_same_on_the_local_path_with_fake_engines(project):
    """SS4: a Discard of the top request does not cut the stack on the local path either."""
    local(project, "req-1", fixes_calc)
    hand_edits(project)
    local(project, "req-2", writes_file("src/extra.py", "EXTRA = 1\n"))
    assert discard_changes("req-2", repo=project)["discarded"] is True
    review = local(project, "req-3", writes_file("src/third.py", "THIRD = 3\n"))
    assert built_on(project, "req-3") == "req-1"
    assert baseline_of(project, "req-3") == baseline_of(project, "req-1")
    assert review["facts"]["files"] == ["src/calc.py", "src/third.py"], "R3 lists the fix and its own file"
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["blocked_by"] == "req-3"
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py", "src/third.py"] and kept["also_kept"] == ["req-1"]
    assert not (project / "src" / "extra.py").exists() and hand_edits_survive(project)
    assert history(project) == [("req-3", "kept"), ("req-2", "discarded"), ("req-1", "kept")]


# --- Check 5: nothing comes back, nothing is wiped, nothing breaks ---------------------------------------------


def test_nothing_comes_back_after_a_discard_not_through_the_copy_a_candidate_a_review_or_a_keep(project):
    """SS6: R2's file is nowhere after the Discard and the retry; with everything discarded the copy is R1's start."""
    pid = project_id_for(project)
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also add an extra module.", adds("src/extra.py", "EXTRA = 1\n", "Added extra.py."))
    assert discard_changes("req-2", repo=project)["discarded"] is True
    review = run(project, "req-3", "Now also document add().", documenting())
    assert not (working_copy(project) / "src" / "extra.py").exists()
    assert not (request_dir(pid, "req-3") / "candidate" / "src" / "extra.py").exists()
    assert "src/extra.py" not in review["facts"]["files"] and "extra.py" not in snapshot("req-3", repo=project)["diff"]
    assert discard_changes("req-3", repo=project)["discarded"] is True
    assert discard_changes("req-1", repo=project)["discarded"] is True
    assert identity(working_copy(project)) == identity(request_dir(pid, "req-1") / "baseline"), (
        "everything discarded: the copy is the folder R1 started from"
    )
    fourth = Script(reply(calls=[call("list", {"path": "."}, "l"), call("list", {"path": "src"}, "s"),
                                 call("read", {"path": "src/calc.py"}, "a")]),
                    add_module("src/other.py", "OTHER = 1\n"), reply("Added other.py."))
    at_first_call: dict = {}
    fourth.hook = lambda: at_first_call.update(record(project, "req-4"))
    last = run(project, "req-4", "Add another module.", fourth)
    assert at_first_call["built_on"] is None and "notes.txt" in at_first_call["baseline_files"], "from the folder"
    assert "BUG" in fourth.result("a") and "extra.py" not in fourth.result("s"), "no trace of the discarded work"
    assert last["facts"]["files"] == ["src/other.py"]
    kept = keep_changes("req-4", project)
    assert kept["applied"] == ["src/other.py"] and kept["also_kept"] == [] and hand_edits_survive(project)
    assert BUG in (project / "src" / "calc.py").read_text(encoding="utf-8"), "the fix never landed"
    assert history(project) == [
        ("req-4", "kept"), ("req-3", "discarded"), ("req-2", "discarded"), ("req-1", "discarded"),
    ]


def test_a_discard_goes_through_when_the_result_the_request_was_built_on_is_gone(project):
    """SS7: R1's candidate/ is deleted. The Discard falls back to the request's own baseline, never an error."""
    pid = project_id_for(project)
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also add an extra module.", adds("src/extra.py", "EXTRA = 1\n", "Added extra.py."))
    shutil.rmtree(request_dir(pid, "req-1") / "candidate")
    assert discard_changes("req-2", repo=project)["discarded"] is True
    third = Script(reply(calls=[call("list", {"path": "src"}, "l")]), add_module("src/third.py", "THIRD = 3\n"),
                   reply("Added third.py."))
    at_first_call: dict = {}
    third.hook = lambda: at_first_call.update(record(project, "req-3"))
    review = run(project, "req-3", "Add a third module.", third)
    assert at_first_call["built_on"] is None, "nothing whole waits: the request starts from the folder"
    assert "extra.py" not in third.result("l") and review["facts"]["files"] == ["src/third.py"]


def test_discarding_a_request_with_no_files_of_its_own_leaves_the_result_in_the_copy(project):
    """SS8: an earlier request only answered. Its Discard must not wipe the waiting fix from the working copy."""
    run(project, "req-0", "Make it nicer.", Script(reply("Which part do you mean: the code or the tests?")))
    answered = snapshot("req-0", repo=project)
    assert answered["facts"]["files"] == [] and answered["can_keep"] is False
    fix(project)
    assert discard_changes("req-0", repo=project)["discarded"] is True
    still_there = (working_copy(project) / "src" / "calc.py").read_text(encoding="utf-8")
    assert FIXED in still_there, "R1's fix is still in the copy"
    second = documenting()
    at_first_call: dict = {}
    second.hook = lambda: at_first_call.update(record(project, "req-2"))
    review = run(project, "req-2", "Now also document add().", second)
    assert at_first_call["built_on"] == "req-1" and FIXED in second.result("a") and "BUG" not in second.result("a")
    assert review["facts"]["files"] == ["src/calc.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]


def test_a_crashed_run_leaves_no_stray_file_in_the_next_request_and_holds_nothing_back(project):
    """ST1: the local engine writes a stray file and raises. The stack is cut, safely: the stray is in no review."""
    local(project, "req-1", fixes_calc)

    def crashes(cwd: Path, _text: str, _cancel) -> None:
        (cwd / "src" / "stray.py").write_text("STRAY = 1\n", encoding="utf-8")
        raise RuntimeError("the engine fell over")

    with pytest.raises(RuntimeError):
        local(project, "req-2", crashes)
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None, "the crashed run holds R1 back no longer"
    review = local(project, "req-3", writes_file("src/third.py", "THIRD = 3\n"))
    assert "src/stray.py" not in review["facts"]["files"]
    kept = keep_changes("req-3", project)
    assert "src/stray.py" not in kept["applied"] and not (project / "src" / "stray.py").exists()


def test_a_request_after_the_discard_of_an_unstacked_request_starts_from_the_folder(project):
    """GU1: the copy goes back to the folder R1 started from, and the discarded fix does not come back."""
    fix(project)
    assert discard_changes("req-1", repo=project)["discarded"] is True
    (project / "notes.txt").write_text(NICO_NOTES, encoding="utf-8")
    second = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "a"), call("list", {"path": "."}, "l")]),
                    add_module("src/extra.py", "EXTRA = 1\n"), reply("Added extra.py."))
    at_first_call: dict = {}
    second.hook = lambda: at_first_call.update(record(project, "req-2"))
    review = run(project, "req-2", "Add an extra module.", second)
    assert at_first_call["built_on"] is None and "notes.txt" in at_first_call["baseline_files"]
    assert "BUG" in second.result("a"), "the discarded fix does not come back"
    assert review["facts"]["files"] == ["src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/extra.py"] and kept["also_kept"] == []


def test_discarding_an_older_request_while_a_newer_independent_result_is_in_the_copy(project):
    """x2: R3 crashes and cuts the stack, R4 starts from the folder, then he discards R2. The Discard restores the copy
    to R1's result, so R5 starts from the folder, and every Keep stays exact."""
    local(project, "req-1", writes_file("src/a.py", "A = 1\n"))
    local(project, "req-2", writes_file("src/extra.py", "EXTRA = 1\n"))

    def crashes(cwd: Path, _text: str, _cancel) -> None:
        (cwd / "src" / "stray.py").write_text("STRAY = 1\n", encoding="utf-8")
        raise RuntimeError("the engine fell over")

    with pytest.raises(RuntimeError):
        local(project, "req-3", crashes)
    local(project, "req-4", writes_file("src/four.py", "FOUR = 4\n"))
    assert built_on(project, "req-4") is None, "the stray cut the stack: R4 starts from the folder"
    assert discard_changes("req-2", repo=project)["discarded"] is True
    review = local(project, "req-5", writes_file("src/five.py", "FIVE = 5\n"))
    assert built_on(project, "req-5") is None and review["facts"]["files"] == ["src/five.py"]
    assert keep_changes("req-5", project)["applied"] == ["src/five.py"]
    assert keep_changes("req-4", project)["applied"] == ["src/four.py"]
    kept = keep_changes("req-1", project)
    assert kept["applied"] == ["src/a.py"] and kept["also_kept"] == []
    assert sorted(p.name for p in (project / "src").iterdir() if p.suffix == ".py") == [
        "__init__.py", "a.py", "calc.py", "five.py", "four.py"], "no extra.py, no stray.py"
    assert history(project) == [
        ("req-5", "kept"), ("req-4", "kept"), ("req-3", "review"), ("req-2", "discarded"), ("req-1", "kept"),
    ]


# --- Check 4: a follow-up that changes anything is still a real stack ---------------------------------------


def test_a_follow_up_that_adds_one_empty_file_is_a_real_stack(project):
    """ID5: it lists R1's file and its own, offers Keep and Discard, and holds R1 back."""
    fix(project)
    review = run(project, "req-2", "Add an empty marker file.", adds("src/marker.py", "", "Added marker.py."))
    assert review["facts"]["files"] == ["src/calc.py", "src/marker.py"]
    second = snapshot("req-2", repo=project)
    assert second["can_keep"] is True and second["can_discard"] is True
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["blocked_by"] == "req-2" and older["summary"] == NEWER_FIRST
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/marker.py"] and kept["also_kept"] == ["req-1"]


def test_a_follow_up_that_adds_a_module_is_kept_with_the_older_request_and_his_edits_survive(project):
    """GU5: listed together, one Keep."""
    fix(project)
    hand_edits(project)
    review = run(project, "req-2", "Now also add an extra module.", adds("src/extra.py", "EXTRA = 1\n"))
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["blocked_by"] == "req-2" and older["summary"] == NEWER_FIRST
    assert keep_changes("req-2", project)["also_kept"] == ["req-1"]
    assert hand_edits_survive(project)


def test_a_follow_up_that_is_still_running_holds_the_older_request_back_in_plain_words(project):
    """GU4: mid-run, the older result offers neither button and a Keep or Discard of it is refused as busy."""
    fix(project)
    folder = tree(project)
    mid: dict = {}

    def while_running() -> None:
        mid["snapshot"] = snapshot("req-1", repo=project)
        mid["keep"] = refusal(lambda: keep_changes("req-1", project))
        mid["discard"] = refusal(lambda: discard_changes("req-1", repo=project))
        mid["folder_untouched"] = tree(project) == folder

    second = adds("src/extra.py", "EXTRA = 1\n", "Added extra.py.")
    second.hook = while_running
    run(project, "req-2", "Now also add an extra module.", second)
    during = mid["snapshot"]
    assert during["can_keep"] is False and during["can_discard"] is False
    assert during["blocked_by"] == "req-2" and during["summary"] == NEWER_RUNNING
    assert mid["keep"] == ("busy", NEWER_RUNNING) and mid["discard"] == ("busy", STILL_RUNNING)
    assert mid["folder_untouched"] is True
    assert keep_changes("req-2", project)["also_kept"] == ["req-1"]


# --- Check 8: unchanged corners ---------------------------------------------------------------------------------


def test_a_follow_up_that_undoes_the_older_request_is_unchanged(project):
    """GU2: its result equals the folder, so it has no files; R1 stays keepable and the next request starts from
    the folder as it is now (undoing R1 is not something to build on)."""
    fix(project)
    undo = Script(reply(calls=[call("edit", {"path": "src/calc.py", "old_string": FIXED, "new_string": BUG}, "u")]),
                  reply("Undone."))
    second = run(project, "req-2", "Undo that fix.", undo)
    assert second["facts"]["files"] == [] and snapshot("req-2", repo=project)["can_keep"] is False
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None
    third = run(project, "req-3", "Add an extra module.", adds("src/extra.py", "EXTRA = 1\n"))
    assert built_on(project, "req-3") is None, "not built on the request that undid R1"
    assert third["facts"]["files"] == ["src/extra.py"]


def test_a_continuation_still_brings_its_own_copy_and_inherits_no_baseline(project):
    """GU3: built on the request it continues, with the folder as its baseline (his notes in it)."""
    fix(project)
    hand_edits(project)
    script = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "a")]),
                    add_module("src/extra.py", "EXTRA = 1\n"), reply("Carried on."))
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(record(project, "req-2"))
    review = run(project, "req-2", "Carry on", script, continue_from="req-1")
    assert at_first_call["built_on"] == "req-1" and "notes.txt" in at_first_call["baseline_files"]
    assert FIXED in script.result("a") and {"src/calc.py", "src/extra.py"} <= set(review["facts"]["files"])


def test_a_later_request_that_removes_a_file_an_earlier_one_created_closes_the_earlier_one(project):
    """Net-out: R2 removes the gen.py that R1 created. Keep R2 takes calc.py only; R1 is closed with that file dropped
    (it used to stay keepable, and its Keep was refused as a conflict that blamed his edit). Keep and Discard of R1
    are refused with the true sentence and the folder is byte-identical after both."""

    def creates_gen(cwd: Path, text: str, cancel) -> None:
        fixes_calc(cwd, text, cancel)
        (cwd / "src" / "gen.py").write_text("GEN = 1\n", encoding="utf-8")

    def removes_gen(cwd: Path, _text: str, _cancel) -> None:
        (cwd / "src" / "gen.py").unlink()

    local(project, "req-1", creates_gen)
    local(project, "req-2", removes_gen)
    assert built_on(project, "req-2") == "req-1"
    second = snapshot("req-2", repo=project)
    assert second["facts"]["files"] == ["src/calc.py"] and second["can_keep"] is True
    older = snapshot("req-1", repo=project)
    assert older["facts"]["files"] == ["src/calc.py", "src/gen.py"] and older["blocked_by"] == "req-2"
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]
    assert history(project) == [("req-2", "kept"), ("req-1", "replaced")]
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["can_discard"] is False and older["blocked_by"] is None
    assert older["kept_with"] == "req-2" and older["dropped"] == ["src/gen.py"] and older["outcome"] == "replaced"
    assert older["summary"] == REPLACED_GEN
    folder = tree(project)
    assert refusal(lambda: keep_changes("req-1", project)) == ("kept", REPLACED_GEN)
    assert refusal(lambda: discard_changes("req-1", repo=project)) == ("kept", REPLACED_GEN)
    assert tree(project) == folder and not (project / "src" / "gen.py").exists()


# --- Check 3: a request that changes nothing of its own lists nothing and holds nothing back ----------------------


def assert_no_result_of_its_own(project: Path, request_id: str = "req-2", older_id: str = "req-1") -> None:
    """The follow-up lists no file and offers neither button; the older result is free and still `review`."""
    follow_up, older = snapshot(request_id, repo=project), snapshot(older_id, repo=project)
    assert follow_up["facts"]["files"] == [] and follow_up["diff"] == ""
    assert follow_up["can_keep"] is False and follow_up["can_discard"] is False and follow_up["blocked_by"] is None
    assert older["can_keep"] is True and older["can_discard"] is True and older["blocked_by"] is None
    assert older["outcome"] == "review" and older["summary"] != NEWER_FIRST


def test_a_follow_up_whose_model_fails_lists_nothing_and_a_plain_retry_builds_on_the_result_that_waits(project):
    """ID1, with no Discard: the failed R2 says what happened and holds R1 back no longer; the retry is built on R1."""
    pid = project_id_for(project)
    fix(project)
    hand_edits(project)
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    run(project, "req-2", "Now also document add().", fails())
    failed = snapshot("req-2", repo=project)
    assert failed["outcome"] == "error"
    assert "The model did not answer: the provider dropped the stream" in failed["summary"]
    assert_no_result_of_its_own(project)
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder, "the failed run wrote nothing"
    note = waiting_note(project, can_read=True)
    assert "src/calc.py" in note and R1_TITLE in note, "a question is told about R1's waiting result"
    retry = documenting()
    at_first_call: dict = {}
    retry.hook = lambda: at_first_call.update(record(project, "req-3"))
    review = run(project, "req-3", "Now also document add().", retry)
    assert at_first_call["built_on"] == "req-1"
    assert FIXED in retry.result("a") and "BUG" not in retry.result("a"), "the retry reads the fix"
    assert review["facts"]["files"] == ["src/calc.py"]
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"], "only R1 is closed, not the failed R2"
    again = snapshot("req-2", repo=project)
    assert again["outcome"] == "error" and again["summary"] != WENT_IN, "R2 keeps telling the truth"
    assert history(project) == [("req-3", "kept"), ("req-2", "error"), ("req-1", "kept")]
    assert hand_edits_survive(project)


def test_a_follow_up_stopped_before_it_wrote_says_nothing_was_changed(project):
    """ID2: it used to say 'Review what changed so far, or discard it.' although it wrote nothing."""
    pid = project_id_for(project)
    fix(project)
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    cancel = threading.Event()
    second = adds("src/extra.py", "EXTRA = 1\n", "Added extra.py.")
    second.hook = cancel.set
    run(project, "req-2", "Now also add an extra module.", second, cancel_event=cancel)
    stopped = snapshot("req-2", repo=project)
    assert stopped["outcome"] == "stopped" and stopped["summary"] == STOPPED_NOTHING
    assert_no_result_of_its_own(project)
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder


@pytest.mark.parametrize("how", ["answered", "same-text"])
def test_a_follow_up_that_only_answers_or_rewrites_the_same_text_lists_nothing(project, how):
    """ID3: a question back, or `write` of a file with the very text it already has."""
    fix(project)
    if how == "answered":
        script = Script(reply("Which part do you mean: the code or the tests?"))
    else:
        current = (working_copy(project) / "src" / "calc.py").read_text(encoding="utf-8")
        script = Script(reply(calls=[call("write", {"path": "src/calc.py", "content": current}, "w")]),
                        reply("Rewrote calc.py."))
    run(project, "req-2", "Tidy calc.py.", script)
    assert_no_result_of_its_own(project)
    if how == "answered":
        assert snapshot("req-2", repo=project)["summary"] == "Which part do you mean: the code or the tests?"


def test_a_local_engine_that_changes_nothing_between_two_requests_is_not_part_of_the_stack(project):
    """ID4: R2 has no files; R3 is built on R1 and one Keep takes both; R2 stays a request that changed nothing."""
    local(project, "req-1", fixes_calc)
    hand_edits(project)
    local(project, "req-2", lambda _cwd, _text, _cancel: None)
    assert_no_result_of_its_own(project)
    review = local(project, "req-3", writes_file("src/third.py", "THIRD = 3\n"))
    assert built_on(project, "req-3") == "req-1"
    assert review["facts"]["files"] == ["src/calc.py", "src/third.py"]
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py", "src/third.py"] and kept["also_kept"] == ["req-1"]
    assert history(project) == [("req-3", "kept"), ("req-2", "review"), ("req-1", "kept")]
    assert hand_edits_survive(project)


def test_an_idle_follow_up_does_not_stop_the_older_request_being_kept_alone(project):
    """ID6: R2 is idle, R1 is kept on its own, R2 is discarded, and the next request starts from the folder."""
    fix(project)
    hand_edits(project)
    run(project, "req-2", "Now also document add().", fails())
    assert keep_changes("req-1", project)["applied"] == ["src/calc.py"]
    assert discard_changes("req-2", repo=project)["discarded"] is True
    third = Script(reply(calls=[call("list", {"path": "."}, "l"), call("read", {"path": "src/calc.py"}, "a")]),
                   add_module("src/extra.py", "EXTRA = 1\n"), reply("Added extra.py."))
    at_first_call: dict = {}
    third.hook = lambda: at_first_call.update(record(project, "req-3"))
    review = run(project, "req-3", "Now also add an extra module.", third)
    assert at_first_call["built_on"] is None and "notes.txt" in at_first_call["baseline_files"]
    assert FIXED in third.result("a"), "the kept fix is in the folder its agent reads"
    assert review["facts"]["files"] == ["src/extra.py"]
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/extra.py"] and kept["also_kept"] == [] and hand_edits_survive(project)
    assert history(project) == [("req-3", "kept"), ("req-2", "discarded"), ("req-1", "kept")]


def test_a_discard_of_an_idle_follow_up_through_the_helper_keeps_the_stack(project):
    """ID8: the app offers no button for it, but the helper accepts the call; the retry is still built on R1."""
    fix(project)
    run(project, "req-2", "Now also document add().", fails())
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    reply_ = server.dispatch({"id": "1", "method": "discard", "params": {"episode_id": "req-2", "path": str(project)}})
    assert reply_["ok"] is True and reply_["result"]["discarded"] is True
    retry = documenting()
    at_first_call: dict = {}
    retry.hook = lambda: at_first_call.update(record(project, "req-3"))
    run(project, "req-3", "Now also document add().", retry)
    assert at_first_call["built_on"] == "req-1" and FIXED in retry.result("a")
    assert keep_changes("req-3", project)["also_kept"] == ["req-1"]


def test_a_hosted_failure_after_a_local_request_and_the_hosted_retry_build_on_the_local_result(project):
    """ID10: the engines may differ along a stack."""
    local(project, "req-1", fixes_calc)
    run(project, "req-2", "Now also document add().", fails())
    assert_no_result_of_its_own(project)
    retry = documenting()
    at_first_call: dict = {}
    retry.hook = lambda: at_first_call.update(record(project, "req-3"))
    run(project, "req-3", "Now also document add().", retry)
    assert at_first_call["built_on"] == "req-1" and FIXED in retry.result("a")
    assert keep_changes("req-3", project)["also_kept"] == ["req-1"]
    assert history(project) == [("req-3", "kept"), ("req-2", "error"), ("req-1", "kept")]


def test_a_local_model_that_returns_no_file_blocks_has_no_result_of_its_own(project):
    """ID11: the file-block path; `fake_complete` stands in for the model."""
    local(project, "req-1", fixes_calc)
    run_general_request(project, "Make it nicer.", request_id="req-2", start_preview_after=False,
                        fake_complete=lambda _prompt: "I could not find anything to change.")
    assert_no_result_of_its_own(project)
    review = local(project, "req-3", writes_file("src/third.py", "THIRD = 3\n"))
    assert built_on(project, "req-3") == "req-1" and review["facts"]["files"] == ["src/calc.py", "src/third.py"]
    assert keep_changes("req-3", project)["also_kept"] == ["req-1"]


def test_a_local_run_that_raises_before_it_wrote_anything_does_not_cut_the_stack(project):
    """ID13: the third request is built on R1, not on nothing, and one Keep takes both."""
    local(project, "req-1", fixes_calc)

    def raises(_cwd: Path, _text: str, _cancel) -> None:
        raise RuntimeError("the engine fell over")

    with pytest.raises(RuntimeError):
        local(project, "req-2", raises)
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None
    review = local(project, "req-3", writes_file("src/third.py", "THIRD = 3\n"))
    assert built_on(project, "req-3") == "req-1" and review["facts"]["files"] == ["src/calc.py", "src/third.py"]
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py", "src/third.py"] and kept["also_kept"] == ["req-1"]


def test_a_follow_up_whose_baseline_folder_is_damaged_while_it_runs_does_not_list_the_project_as_deleted(project):
    """ID12: publishing an empty baseline would make the candidate empty and list every file as deleted."""
    pid = project_id_for(project)
    fix(project)
    second = fails()
    second.hook = lambda: shutil.rmtree(request_dir(pid, "req-2") / "baseline")
    run(project, "req-2", "Now also document add().", second)
    assert set(snapshot("req-2", repo=project)["facts"]["files"]) <= {"src/calc.py"}


def test_a_continuation_that_changes_nothing_is_never_idle(project):
    """`continue_from` brings its own copy: it lists the continued result as its own, as before."""
    fix(project)
    review = run_general_request(project, "Carry on", request_id="req-2", continue_from="req-1",
                                 fake_engine=lambda _cwd, _text, _cancel: None, start_preview_after=False)
    assert built_on(project, "req-2") == "req-1"
    assert review["facts"]["files"] == ["src/calc.py"]


def test_a_running_request_is_never_passed_over(project):
    """While R2 runs it is the request that counts: R3 would not be built on R1 behind it. Once R2 has finished
    with nothing of its own it is passed over, and R1 is the result the copy holds."""
    from local_coder.general import coordinator

    local(project, "req-1", fixes_calc)
    pid = project_id_for(project)
    copy = working_copy(project)
    seen: list[str | None] = []
    local(project, "req-2", lambda _cwd, _text, _cancel: seen.append(coordinator._open_request_in(pid, "req-3", copy)))
    assert seen == [None], "R2 is running: it is not passed over"
    assert coordinator._open_request_in(pid, "req-3", copy) == "req-1", "finished and idle: passed over"


# --- Check 6: a question is told the truth ------------------------------------------------------------------------


def test_a_question_still_finds_the_waiting_result_after_many_failed_follow_ups(project):
    """ID7: an outage. Seven failed follow-ups in a row must not hide R1 from `find_waiting_result`."""
    fix(project)
    for number in range(2, 9):
        run(project, f"req-{number}", "Now also document add().", fails())
        waiting = find_waiting_result(project)
        assert waiting is not None and waiting.request_id == "req-1", f"after req-{number}"
    note = waiting_note(project, can_read=True)
    assert "src/calc.py" in note and R1_TITLE in note
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None


# --- Check 7: the daily-driver shapes: a Vite app and a Babel project ---------------------------------------------

STARTER_TEXT = "<h1>New app</h1>\n      <p>This starter is empty on purpose. Describe what you want to build.</p>"
BABEL_PACKAGE = {
    "name": "babel-app", "private": True, "version": "0.0.0", "type": "module",
    "scripts": {"dev": "vite", "build": "tsc -b && vite build"},
    "dependencies": {"react": "^19.1.1", "react-dom": "^19.1.1"},
    "devDependencies": {
        "@types/react": "^19.1.12", "@vitejs/plugin-react": "^5.0.2", "typescript": "~5.9.2", "vite": "^7.1.5",
    },
}
BABEL_VITE = (
    'import { defineConfig } from "vite";\nimport react from "@vitejs/plugin-react";\n\n'
    "export default defineConfig({\n  plugins: [react()],\n});\n"
)
BABEL_APP = "export default function App() {\n  return (\n    <main>\n      <h1>Hello</h1>\n    </main>\n  );\n}\n"
BABEL_MAIN = (
    'import { createRoot } from "react-dom/client";\nimport App from "./App";\n'
    'createRoot(document.getElementById("root")!).render(<App />);\n'
)
BABEL_INDEX = (
    '<!doctype html><html><body><div id="root"></div>'
    '<script type="module" src="/src/main.tsx"></script></body></html>\n'
)


def edit_app(old: str, new: str, call_id: str = "e") -> dict:
    return reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": old, "new_string": new}, call_id)])


def test_a_failed_follow_up_on_a_vite_app_runs_no_check_and_the_retry_is_checked_as_a_whole(home):
    """ID9: it used to list src/App.tsx, run Local Coder's checks and record `checks_passed` for a failed model."""
    folder = home / "Local Coder Projects" / "board"
    folder.mkdir(parents=True)
    pid = project_id_for(folder)
    run(folder, "req-1", "Make the page show Tasks",
        Script(edit_app(STARTER_TEXT, "<h1>Tasks</h1>", "1"), reply("The page now says Tasks.")))
    assert load_meta(pid, "req-1")["stop_reason"] == "checks_passed"
    run(folder, "req-2", "Now also make it blue", fails())
    second = load_meta(pid, "req-2")
    assert snapshot("req-2", repo=folder)["facts"]["files"] == []
    assert not second.get("checks") and second.get("stop_reason") != "checks_passed", "no check ran for it"
    older = snapshot("req-1", repo=folder)
    assert older["can_keep"] is True and older["blocked_by"] is None

    def make_it_blue(seen: str) -> dict:
        old = "<h1>Tasks</h1>" if "<h1>Tasks</h1>" in seen else STARTER_TEXT
        return edit_app(old, "<h1 style={{ color: 'blue' }}>Tasks</h1>")

    retry = Script(reply(calls=[call("read", {"path": "src/App.tsx"}, "a")]), make_it_blue,
                   reply("The heading is blue."))
    at_first_call: dict = {}
    retry.hook = lambda: at_first_call.update(record(folder, "req-3"))
    review = run(folder, "req-3", "Now also make it blue", retry)
    assert at_first_call["built_on"] == "req-1" and "<h1>Tasks</h1>" in retry.result("a")
    assert load_meta(pid, "req-3")["stop_reason"] == "checks_passed", "the retry is checked as a whole"
    assert review["facts"]["files"] == ["src/App.tsx"]
    kept = keep_changes("req-3", folder)
    assert kept["applied"] == ["src/App.tsx"] and kept["also_kept"] == ["req-1"]
    assert "blue" in (folder / "src" / "App.tsx").read_text(encoding="utf-8")


def test_a_babel_vite_project_keeps_package_json_and_vite_config_out_of_every_review_after_a_failure(home):
    """BB1: R1 changes the heading, R2 fails, he discards R2, R3 retries. Hosted requests never list the SWC rewrite."""
    babel = home / "projects" / "babel-app"
    (babel / "src").mkdir(parents=True)
    (babel / "package.json").write_text(json.dumps(BABEL_PACKAGE, indent=2) + "\n", encoding="utf-8")
    (babel / "vite.config.ts").write_text(BABEL_VITE, encoding="utf-8")
    (babel / "index.html").write_text(BABEL_INDEX, encoding="utf-8")
    (babel / "src" / "main.tsx").write_text(BABEL_MAIN, encoding="utf-8")
    (babel / "src" / "App.tsx").write_text(BABEL_APP, encoding="utf-8")
    pid = project_id_for(babel)
    configs = {name: (babel / name).read_bytes() for name in ("package.json", "vite.config.ts")}
    first = run(babel, "req-1", "Make the heading say Tasks",
                Script(edit_app("<h1>Hello</h1>", "<h1>Tasks</h1>"), reply("Heading now says Tasks.")))
    run(babel, "req-2", "Now also make it blue", fails())
    assert first["facts"]["files"] == ["src/App.tsx"] and snapshot("req-2", repo=babel)["facts"]["files"] == []
    older = snapshot("req-1", repo=babel)
    assert older["can_keep"] is True and older["blocked_by"] is None
    assert discard_changes("req-2", repo=babel)["discarded"] is True

    def blue(seen: str) -> dict:
        old = "<h1>Tasks</h1>" if "<h1>Tasks</h1>" in seen else "<h1>Hello</h1>"
        return edit_app(old, "<h1 style={{ color: 'blue' }}>Tasks</h1>")

    retry = Script(reply(calls=[call("read", {"path": "src/App.tsx"}, "a")]), blue, reply("Blue."))
    at_first_call: dict = {}
    retry.hook = lambda: at_first_call.update(record(babel, "req-3"))
    review = run(babel, "req-3", "Now also make it blue", retry)
    assert at_first_call["built_on"] == "req-1" and "<h1>Tasks</h1>" in retry.result("a")
    assert review["facts"]["files"] == ["src/App.tsx"]
    candidate = tree(request_dir(pid, "req-3") / "candidate")
    assert {name: candidate[name] for name in configs} == configs, "the candidate keeps the project's own config"
    kept = keep_changes("req-3", babel)
    assert kept["applied"] == ["src/App.tsx"] and kept["also_kept"] == ["req-1"]
    assert {name: (babel / name).read_bytes() for name in configs} == configs


# --- The Challenger's extra checks ---------------------------------------------------------------------------------


def test_a_local_model_that_re_emits_a_file_unchanged_is_idle_costs_one_model_call_and_runs_no_check(home):
    """x1b: `apply_files` counts an identical rewrite as written. The follow-up used to run Local Coder's checks on
    the baseline copy, fail 'the preview is still the empty starter page' and spend three model calls on it."""
    folder = home / "Local Coder Projects" / "board"
    folder.mkdir(parents=True)
    pid = project_id_for(folder)
    app = "export default function App(){return <main><h1>Tasks</h1></main>}\n"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return f"Done.\n\n```src/App.tsx\n{app}```\n"

    kwargs = {"model_id": "Qwen3.5-4B-Q4_K_M", "fake_complete": model, "start_preview_after": False}
    first = run_general_request(folder, "Make the page show Tasks", request_id="req-1", **kwargs)
    asked_for_the_first = len(prompts)
    run_general_request(folder, "Make the page show Tasks", request_id="req-2", **kwargs)
    second = load_meta(pid, "req-2")
    assert first["facts"]["files"] == ["src/App.tsx"] and load_meta(pid, "req-1")["stop_reason"] == "checks_passed"
    assert second["built_on"] == "req-1"
    assert len(prompts) - asked_for_the_first == 1, "one model call, no repair rounds"
    assert not second.get("checks") and second.get("stop_reason") not in {"no_progress", "checks_passed"}
    assert_no_result_of_its_own(folder)


def test_an_idle_publish_leaves_no_stale_check_text_on_the_card(home):
    """x6: the agent breaks App.tsx, Local Coder's check fails, the agent reverts to R1's exact text. The card must not
    keep 'I wrote 0 files, but the in-app preview does not run ... Keep only if you want these files anyway'."""
    folder = home / "Local Coder Projects" / "board"
    folder.mkdir(parents=True)
    pid = project_id_for(folder)
    good = "export default function App(){return <h1>Tasks</h1>}\n"
    broken = "export default function App(){return <h1>Tasks</h1\n"
    run(folder, "req-1", "Make the page show Tasks",
        Script(reply(calls=[call("write", {"path": "src/App.tsx", "content": good}, "w")]), reply("Done.")))
    second = Script(
        reply(calls=[call("write", {"path": "src/App.tsx", "content": broken}, "w")]), reply("Done."),
        reply(calls=[call("write", {"path": "src/App.tsx", "content": good}, "w")]), reply("Reverted."),
    )
    run(folder, "req-2", "Now make it blue", second)
    meta = load_meta(pid, "req-2")
    assert meta.get("checks"), "the check did run on the broken state"
    card = snapshot("req-2", repo=folder)
    assert card["facts"]["files"] == [] and card["can_keep"] is False and card["can_discard"] is False
    assert card["summary"] == "Reverted."
    assert "preview does not run" not in json.dumps(card) and "Keep only if" not in json.dumps(card)
    assert card["verification"]["preview"] == "not_run" and card["verification"]["typecheck"] == "not_run"
    assert "verification" not in meta and "preview_health" not in meta
    older = snapshot("req-1", repo=folder)
    assert older["can_keep"] is True and older["blocked_by"] is None


def test_a_visible_junk_file_still_makes_a_follow_up_a_real_stack(project):
    """x3, the known limit: a hosted follow-up that leaves `.coverage` is not idle. It lists what it did before."""
    fix(project)
    run(project, "req-2", "Run the tests and tell me.", Script(add_module(".coverage", "x"), reply("All good.")))
    second, older = snapshot("req-2", repo=project), snapshot("req-1", repo=project)
    assert second["facts"]["files"] == [".coverage", "src/calc.py"] and second["can_keep"] is True
    assert older["can_keep"] is False and older["blocked_by"] == "req-2"


def test_a_follow_up_that_touches_only_files_the_snapshot_ignores_is_idle(project):
    """x7: node_modules, dist and package-lock.json are not part of any snapshot."""
    fix(project)
    run(project, "req-2", "Install something.", Script(
        reply(calls=[call("write", {"path": "node_modules/x/index.js", "content": "1"}, "w1"),
                     call("write", {"path": "dist/out.js", "content": "1"}, "w2"),
                     call("write", {"path": "package-lock.json", "content": "{}"}, "w3")]),
        reply("Installed."),
    ))
    assert_no_result_of_its_own(project)


def test_discarding_the_older_request_while_an_idle_follow_up_waits_behind_it_is_allowed(project):
    """x5: R1 is discarded, the idle R2 stays, the next request starts from the folder and a question hears nothing."""
    fix(project)
    run(project, "req-2", "Now also document add().", fails())
    assert discard_changes("req-1", repo=project)["discarded"] is True
    assert waiting_note(project, can_read=True) == ""
    look = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "a")]), reply("Fine."))
    at_first_call: dict = {}
    look.hook = lambda: at_first_call.update(record(project, "req-3"))
    run(project, "req-3", "Just look at add().", look)
    assert at_first_call["built_on"] is None, "R1 was discarded: nothing waits to be built on"
    assert "BUG" in look.result("a"), "the discarded fix is not in the copy"
    assert history(project) == [("req-3", "review"), ("req-2", "error"), ("req-1", "discarded")]


def test_local_repair_that_restores_r1_text_is_idle(home):
    """A local repair that writes R1's exact text back leaves nothing to check: no second check, no extra calls."""
    import local_coder

    folder = home / "Local Coder Projects" / "board"
    folder.mkdir(parents=True)
    pid = project_id_for(folder)
    starter_app = (
        Path(local_coder.__file__).parent.parent.parent / "starters" / "vite-react-ts" / "src" / "App.tsx"
    ).read_text()
    good = "export default function App(){return <main><h1>Tasks</h1></main>}\n"
    replies = iter(
        [f"Done.\n\n```src/App.tsx\n{good}```\n", f"Done.\n\n```src/App.tsx\n{starter_app}```\n"]
        + [f"Fixed.\n\n```src/App.tsx\n{good}```\n"] * 6
    )
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    kwargs = {"model_id": "Qwen3.5-4B-Q4_K_M", "fake_complete": model, "start_preview_after": False}
    run_general_request(folder, "Make the page show Tasks", request_id="req-1", **kwargs)
    before = len(prompts)
    run_general_request(folder, "Make it blue", request_id="req-2", **kwargs)
    meta = load_meta(pid, "req-2")
    assert len(prompts) - before == 2
    assert len(meta.get("checks") or []) == 1
    assert "verification" not in meta
    assert_no_result_of_its_own(folder)
