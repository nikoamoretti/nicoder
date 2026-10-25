"""Cut hosted-follow-up: on the hosted model a follow-up builds on the result that still waits for Keep.

Talk and act run Local Coder's own agent on a hosted model. Like every other engine, that agent's request finds
the result that still waits in the project's sticky working copy and is built on it (`built_on`): its baseline is
that result's baseline, its agent starts in the copy as that result left it, its review lists both requests'
files, and one Keep takes both and closes the older request. Before, the follow-up read the folder (the old
code), edited the old line, and the older request's Keep could then only fail.

A scripted model (`agent_chat`, $0) stands in for the hosted one; the workspace, baseline, tools, access rules,
Keep, Discard and History are the real code. Each section names the acceptance check it belongs to.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_coder import machine
from local_coder.agent import runner
from local_coder.app_service import chat_request, discard_changes, keep_changes, snapshot
from local_coder.general import coordinator
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
from local_coder.general.starter import starter_root
from local_coder.general.waiting import waiting_note

FIXTURE = Path(__file__).parent.parent / "fixtures" / "demo_calc" / "repo"
BUG = "return a - b  # BUG: subtracts instead of adding"
FIXED = "return a + b"
DOCUMENTED = FIXED + "  # documented"
NICO_TEST_LINE = "# Nico's own edit while the result waits\n"
NICO_NOTES = "Nico's notes\n"
WENT_IN = "These changes went in with a later request you kept."
NEWER_FIRST = "A newer request in this project builds on this one, so Keep or Discard the newer one first."
NEWER_RUNNING = (
    "A newer request in this project builds on this one and is still running, so let it finish or stop it first."
)
STILL_RUNNING = "A request is still running in this project. Wait for it to finish, or stop it, then discard."
OUTSIDE = "outside the project's working copy"
STARTER_TEXT = "<h1>New app</h1>\n      <p>This starter is empty on purpose. Describe what you want to build.</p>"
STARTER_FILES = sorted(index_tree(starter_root()).files)


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


# --- A scripted hosted model and the things the scenarios look at ---------------------------------------------


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A scripted hosted model. A step is a reply, an error to raise, or a function of what its last tool said."""

    def __init__(self, *steps) -> None:
        self.steps, self.sent, self.hook = list(steps), [], None

    def __call__(self, messages: list[dict], _tools: list[dict]) -> dict:
        if not self.sent and self.hook:
            self.hook()  # the agent's first model call: the request is still being made
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


def fix(project: Path, request_id: str = "req-1") -> dict:
    """The first request: the agent reads calc.py and fixes add()."""
    script = Script(
        reply(calls=[call("read", {"path": "src/calc.py"}, "a")]),
        reply(calls=[call("edit", {"path": "src/calc.py", "old_string": BUG, "new_string": FIXED}, "b")]),
        reply("Fixed add()."),
    )
    return run(project, request_id, "Fix add so it adds instead of subtracting.", script)


def document(seen: str) -> dict:
    """A model edits what it saw: the fixed line when the waiting fix is there, the buggy line when it is not."""
    old = FIXED if FIXED in seen else BUG
    return reply(calls=[call("edit", {"path": "src/calc.py", "old_string": old, "new_string": DOCUMENTED}, "d")])


def add_module(path: str, body: str, *more: dict) -> dict:
    return reply(calls=[call("write", {"path": path, "content": body}, "w"), *more])


def add_extra(project: Path, request_id: str = "req-2") -> dict:
    script = Script(add_module("src/extra.py", "EXTRA = 1\n"), reply("Added extra.py."))
    return run(project, request_id, "Now also add an extra module.", script)


def add_third(project: Path, request_id: str = "req-3") -> dict:
    script = Script(add_module("src/third.py", "THIRD = 3\n"), reply("Added third.py."))
    return run(project, request_id, "And a third one.", script)


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


# --- Check 1: the follow-up's agent works on the waiting result (the gap) -------------------------------------


@pytest.fixture
def documented(project):
    """R1 fixes add(); while it waits, R2 'Now also document add().' on the same file."""
    first = fix(project)
    pid = project_id_for(project)
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    script = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "c")]), document, reply("Documented add()."))
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(record(project, "req-2"), working_copy=identity(working_copy(project)))
    second = run(project, "req-2", "Now also document add().", script)
    return SimpleNamespace(project=project, pid=pid, first=first, second=second, script=script,
                           at_first_call=at_first_call, folder=folder, r1_folder=r1_folder)


def test_the_follow_ups_agent_reads_the_fix_that_still_waits(documented):
    """HF1: it reads `return a + b`, not the BUG line, although the chat it is handed already says R1 was done."""
    read = documented.script.result("c")
    assert FIXED in read and "BUG" not in read, "it reads the code as the waiting result left it"
    handed = documented.script.sent[0]
    assert any(m["role"] == "assistant" and "Fixed add()" in m["content"] for m in handed)
    assert documented.at_first_call["built_on"] == "req-1", "built on the waiting result before the first model call"
    waiting_candidate = request_dir(documented.pid, "req-1") / "candidate"
    assert documented.at_first_call["working_copy"] == identity(waiting_candidate), "it starts in R1's result"
    assert documented.second["facts"]["files"] == ["src/calc.py"]
    diff = documented.second["diff"]
    assert f"-    {BUG}" in diff and f"+    {DOCUMENTED}" in diff, "one review shows both changes"


def test_the_follow_up_starts_from_the_baseline_of_the_waiting_result(documented):
    """One starting point for the stack: the same index and the same files, byte for byte."""
    assert baseline_of(documented.project, "req-2") == baseline_of(documented.project, "req-1")
    request_2 = load_meta(documented.pid, "req-2")
    assert request_2["built_on"] == "req-1" and request_2["engine"] == "agent"


def test_nothing_reaches_the_folder_or_the_older_request_while_the_follow_up_waits(documented):
    assert tree(documented.project) == documented.folder, "the folder changes only on Keep"
    assert tree(request_dir(documented.pid, "req-1")) == documented.r1_folder, "R1's own record is evidence"


def test_the_older_request_waits_for_the_follow_up(documented):
    older = snapshot("req-1", repo=documented.project)
    assert older["can_keep"] is False and older["can_discard"] is False
    assert older["blocked_by"] == "req-2" and older["summary"] == NEWER_FIRST
    assert older["facts"]["files"] == ["src/calc.py"], "its files and diff still show"


def test_one_keep_takes_both_and_closes_the_older_request(documented):
    kept = keep_changes("req-2", documented.project)
    assert kept["ok"] is True and kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]
    assert DOCUMENTED in (documented.project / "src" / "calc.py").read_text(encoding="utf-8")
    older = snapshot("req-1", repo=documented.project)
    assert older["outcome"] == "kept" and older["summary"] == WENT_IN and older["kept_with"] == "req-2"
    assert older["can_keep"] is False and older["can_discard"] is False
    assert history(documented.project) == [("req-2", "kept"), ("req-1", "kept")]


def test_there_is_no_keep_left_that_can_only_fail(documented):
    keep_changes("req-2", documented.project)
    before = tree(documented.project)
    again = refusal(lambda: keep_changes("req-1", documented.project))
    assert again == ("kept", "These changes are already in your project.")
    assert tree(documented.project) == before, "and it writes nothing"


def test_a_question_while_the_stack_waits_is_told_about_the_newest_request(documented):
    note = waiting_note(documented.project, can_read=True)
    assert "Now also document add()" in note and "src/calc.py" in note
    assert "src/calc.py" in waiting_note(documented.project, can_read=False)
    keep_changes("req-2", documented.project)
    assert waiting_note(documented.project, can_read=True) == ""
    assert waiting_note(documented.project, can_read=False) == ""


def test_the_agents_working_copy_is_a_copy_and_never_a_link_to_the_waiting_result(project):
    """The agent writes into the sticky working copy; R1's own candidate folder is evidence and must stay as it was."""
    fix(project)
    r1_candidate = request_dir(project_id_for(project), "req-1") / "candidate"
    shared: list[str] = []

    def look() -> None:
        for path in sorted(working_copy(project).rglob("*")):
            twin = r1_candidate / path.relative_to(working_copy(project))
            if path.is_file() and twin.is_file() and os.path.samefile(path, twin):
                shared.append(path.relative_to(working_copy(project)).as_posix())

    script = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "c")]), document, reply("Documented."))
    script.hook = look
    run(project, "req-2", "Now also document add().", script)
    assert shared == [], "no file of the working copy is a hard link to R1's candidate"
    assert FIXED in (r1_candidate / "src" / "calc.py").read_text(encoding="utf-8")
    assert DOCUMENTED not in (r1_candidate / "src" / "calc.py").read_text(encoding="utf-8")


def test_a_waiting_result_that_vanishes_stops_the_run_and_leaves_the_working_copy_alone(project, monkeypatch):
    """Syncing the agent's copy from a candidate that is gone would empty the copy that holds the waiting result."""
    fix(project)
    real = coordinator.prepare_workspace

    def lose_the_candidate(*args, **kwargs):
        meta = real(*args, **kwargs)
        shutil.rmtree(request_dir(meta["project_id"], "req-1") / "candidate")
        return meta

    monkeypatch.setattr(coordinator, "prepare_workspace", lose_the_candidate)
    script = Script(reply("Never asked."))
    with pytest.raises(RuntimeError):
        run(project, "req-2", "Now also document add().", script)
    assert script.sent == [], "the model was never called"
    assert FIXED in (working_copy(project) / "src" / "calc.py").read_text(encoding="utf-8")


@pytest.fixture
def other_file(project):
    """R1 fixes add(); he edits a test and adds notes.txt; R2 adds src/extra.py and tries to write into R1's copy."""
    fix(project)
    pid = project_id_for(project)
    hand_edits(project)
    r1_calc = request_dir(pid, "req-1") / "candidate" / "src" / "calc.py"
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    hijack = call("write", {"path": str(r1_calc), "content": "HIJACKED\n"}, "x")
    script = Script(
        reply(calls=[call("list", {"path": "."}, "a"), call("read", {"path": "src/calc.py"}, "b")]),
        add_module("src/extra.py", "EXTRA = 1\n", hijack),
        reply("Added extra.py."),
    )
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(record(project, "req-2"))
    second = run(project, "req-2", "Now also add an extra module.", script)
    return SimpleNamespace(project=project, pid=pid, second=second, script=script, at_first_call=at_first_call,
                           folder=folder, r1_folder=r1_folder, r1_calc=r1_calc)


def test_a_follow_up_on_another_file_reads_the_fix_and_lists_the_stack_not_his_edits(other_file):
    """HF2: the model's work is R1's file and its own; what he edited by hand is neither shown nor reverted."""
    script = other_file.script
    assert FIXED in script.result("b") and "BUG" not in script.result("b")
    assert other_file.at_first_call["built_on"] == "req-1"
    assert other_file.second["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    assert "notes.txt" not in other_file.second["diff"] and "test_calc.py" not in other_file.second["diff"]
    # The known limit of building on the waiting result: what he changed after R1 started is not in its copy.
    assert "notes.txt" not in script.result("a")


def test_a_write_into_the_older_requests_waiting_copy_is_refused(other_file):
    """Check 7: the agent writes only in its own working copy, whatever path it names."""
    refused = other_file.script.result("x")
    assert OUTSIDE in refused and "Only files of the current project can change." in refused
    waiting_calc = other_file.r1_calc.read_text(encoding="utf-8")
    assert FIXED in waiting_calc and "HIJACKED" not in waiting_calc
    assert tree(request_dir(other_file.pid, "req-1")) == other_file.r1_folder, "R1's record is byte-identical"
    assert tree(other_file.project) == other_file.folder, "the folder is byte-identical until Keep"


def test_keeping_a_follow_up_on_another_file_takes_both_files_and_keeps_his_edits(other_file):
    kept = keep_changes("req-2", other_file.project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["req-1"]
    assert hand_edits_survive(other_file.project)
    assert history(other_file.project) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_follow_up_on_a_vite_web_app_is_checked_as_a_whole_with_the_waiting_result(home):
    """HF9: the daily-driver shape. Local Coder's own checks run on the stack and the follow-up reads the heading.

    A web app that already holds the starter's files, as an older build's New Project left it: an empty folder gets
    the starter in the request's working copy instead (the next test), and this one is about the checks and the stack.
    """
    folder = home / "Local Coder Projects" / "board"
    shutil.copytree(starter_root(), folder)
    first = Script(
        reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": STARTER_TEXT,
                                   "new_string": "<h1>Tasks</h1>"}, "1")]),
        reply("The page now says Tasks."),
    )
    run(folder, "req-1", "Make the page show Tasks", first)
    pid = project_id_for(folder)
    before, r1_before = tree(folder), tree(request_dir(pid, "req-1"))

    def make_it_blue(seen: str) -> dict:
        old = "<h1>Tasks</h1>" if "<h1>Tasks</h1>" in seen else STARTER_TEXT
        return reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": old,
                                          "new_string": "<h1 style={{ color: 'blue' }}>Tasks</h1>"}, "e")])

    second = Script(reply(calls=[call("read", {"path": "src/App.tsx"}, "a")]), make_it_blue, reply("It is blue now."))
    at_first_call: dict = {}
    second.hook = lambda: at_first_call.update(record(folder, "req-2"))
    review = run(folder, "req-2", "Now also make it blue", second)
    assert "<h1>Tasks</h1>" in second.result("a"), "the agent read the waiting change, not the starter text"
    assert at_first_call["built_on"] == "req-1"
    assert load_meta(pid, "req-2")["stop_reason"] == "checks_passed", "the checks ran on the whole stack"
    assert review["facts"]["files"] == ["src/App.tsx"]
    older = snapshot("req-1", repo=folder)
    assert older["can_keep"] is False and older["blocked_by"] == "req-2"
    assert tree(folder) == before and tree(request_dir(pid, "req-1")) == r1_before
    kept = keep_changes("req-2", folder)
    assert kept["applied"] == ["src/App.tsx"] and kept["also_kept"] == ["req-1"]
    app = (folder / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "Tasks" in app and "blue" in app
    assert history(folder) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_follow_up_on_a_first_request_in_an_empty_folder_is_checked_as_a_whole_with_the_waiting_result(home):
    """HF9 on an empty folder: the starter is in the first request's working copy, so the follow-up's review lists its
    files with the edits, the folder stays empty until Keep, and one Keep takes both requests."""
    folder = home / "Local Coder Projects" / "board"
    folder.mkdir(parents=True)
    first = Script(
        reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": STARTER_TEXT,
                                   "new_string": "<h1>Tasks</h1>"}, "1")]),
        reply("The page now says Tasks."),
    )
    run(folder, "req-1", "Make the page show Tasks", first)
    pid = project_id_for(folder)
    r1_before = tree(request_dir(pid, "req-1"))

    def make_it_blue(seen: str) -> dict:
        old = "<h1>Tasks</h1>" if "<h1>Tasks</h1>" in seen else STARTER_TEXT
        return reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": old,
                                          "new_string": "<h1 style={{ color: 'blue' }}>Tasks</h1>"}, "e")])

    second = Script(reply(calls=[call("read", {"path": "src/App.tsx"}, "a")]), make_it_blue, reply("It is blue now."))
    at_first_call: dict = {}
    second.hook = lambda: at_first_call.update(record(folder, "req-2"))
    review = run(folder, "req-2", "Now also make it blue", second)
    assert "<h1>Tasks</h1>" in second.result("a"), "the agent read the waiting change, not the starter text"
    assert at_first_call["built_on"] == "req-1" and at_first_call["baseline_files"] == []
    assert load_meta(pid, "req-2")["stop_reason"] == "checks_passed", "the checks ran on the whole stack"
    assert review["facts"]["files"] == STARTER_FILES, "the starter's files, with both requests' edits in App.tsx"
    older = snapshot("req-1", repo=folder)
    assert older["can_keep"] is False and older["blocked_by"] == "req-2"
    assert tree(folder) == {} and tree(request_dir(pid, "req-1")) == r1_before
    kept = keep_changes("req-2", folder)
    assert kept["applied"] == STARTER_FILES and kept["also_kept"] == ["req-1"]
    app = (folder / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "Tasks" in app and "blue" in app
    assert history(folder) == [("req-2", "kept"), ("req-1", "kept")]


# --- Check 10: package.json and vite.config.ts of a Babel project never leak into a review -------------------

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


@pytest.fixture
def babel(home) -> Path:
    """A Vite project on the Babel React plugin: Local Coder's preview setup rewrites it to the SWC plugin."""
    root = home / "projects" / "babel-app"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps(BABEL_PACKAGE, indent=2) + "\n", encoding="utf-8")
    (root / "vite.config.ts").write_text(BABEL_VITE, encoding="utf-8")
    (root / "index.html").write_text(BABEL_INDEX, encoding="utf-8")
    (root / "src" / "main.tsx").write_text(BABEL_MAIN, encoding="utf-8")
    (root / "src" / "App.tsx").write_text(BABEL_APP, encoding="utf-8")
    return root


def test_a_babel_vite_project_keeps_package_json_and_vite_config_out_of_every_review(babel):
    """Check 10. `prepare_workspace` rewrites the working copy to SWC; the agent's own re-sync must undo that for
    every request, whatever it is built on, or the review lists both files and Keep writes them into the project."""
    pid = project_id_for(babel)
    folder = tree(babel)
    configs = {name: folder[name] for name in ("package.json", "vite.config.ts")}
    seen: dict[str, dict] = {}

    def watch(label: str, source) -> object:
        """At the agent's first call: the working copy it starts in, and the tree it should start from."""
        return lambda: seen.update({label: {"working_copy": identity(working_copy(babel)),
                                            "wanted": identity(source())}})

    first = Script(
        reply(calls=[call("read", {"path": "package.json"}, "pkg")]),
        reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": "<h1>Hello</h1>",
                                   "new_string": "<h1>Tasks</h1>"}, "e")]),
        reply("The heading says Tasks."),
    )
    first.hook = watch("req-1", lambda: babel)
    r1 = run(babel, "req-1", "Make the heading say Tasks", first)
    second = Script(
        reply(calls=[call("read", {"path": "package.json"}, "pkg"), call("read", {"path": "src/App.tsx"}, "app")]),
        reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": "<h1>Tasks</h1>",
                                   "new_string": "<h1 style={{ color: 'blue' }}>Tasks</h1>"}, "e")]),
        reply("The heading is blue."),
    )
    second.hook = watch("req-2", lambda: request_dir(pid, "req-1") / "candidate")
    r2 = run(babel, "req-2", "Now also make it blue", second)
    assert seen["req-1"]["working_copy"] == seen["req-1"]["wanted"], "nothing waits: the agent starts from the folder"
    assert seen["req-2"]["working_copy"] == seen["req-2"]["wanted"], "a follow-up starts from the waiting candidate"
    assert load_meta(pid, "req-2")["built_on"] == "req-1"
    assert "plugin-react-swc" not in first.result("pkg") and "plugin-react-swc" not in second.result("pkg")
    assert "<h1>Tasks</h1>" in second.result("app"), "the follow-up reads the waiting change"
    for review, rid in ((r1, "req-1"), (r2, "req-2")):
        assert review["facts"]["files"] == ["src/App.tsx"], f"{rid} lists only what its agent wrote"
        assert "package.json" not in review["diff"] and "vite.config.ts" not in review["diff"]
        assert load_meta(pid, rid)["agent"]["changed"] == ["src/App.tsx"]
        assert load_meta(pid, rid)["stop_reason"] == "checks_passed"
        candidate = tree(request_dir(pid, rid) / "candidate")
        assert {name: candidate[name] for name in configs} == configs, f"{rid}'s candidate keeps the project's configs"
    kept = keep_changes("req-2", babel)
    assert kept["applied"] == ["src/App.tsx"] and kept["also_kept"] == ["req-1"]
    after = tree(babel)
    assert {name: after[name] for name in configs} == configs, "Keep wrote neither package.json nor vite.config.ts"
    assert b"blue" in after["src/App.tsx"]


def test_a_babel_vite_request_after_a_keep_starts_from_the_folder(babel):
    """Check 10, nothing waits: the working copy at the first call equals the folder, configs included."""
    pid = project_id_for(babel)
    first = Script(
        reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": "<h1>Hello</h1>",
                                   "new_string": "<h1>Tasks</h1>"}, "e")]),
        reply("Done."),
    )
    run(babel, "req-1", "Make the heading say Tasks", first)
    keep_changes("req-1", babel)
    folder = tree(babel)
    seen: dict = {}
    second = Script(add_module("src/extra.ts", "export const extra = 1\n"), reply("Added."))
    second.hook = lambda: seen.update(working_copy=identity(working_copy(babel)), folder=identity(babel))
    review = run(babel, "req-2", "Add an extra module", second)
    assert seen["working_copy"] == seen["folder"]
    assert review["facts"]["files"] == ["src/extra.ts"] and load_meta(pid, "req-2").get("built_on") is None
    config_files = ("package.json", "vite.config.ts")
    candidate = tree(request_dir(pid, "req-2") / "candidate")
    assert {name: candidate[name] for name in config_files} == {name: folder[name] for name in config_files}


# --- Check 2: a clash is refused, writes nothing, and can still be decided ------------------------------------


def test_keep_refuses_a_file_he_edited_that_the_stack_changed_and_writes_nothing(project):
    """HF3a: he edits src/calc.py, which R1 changed, then R2 adds src/extra.py. R2's Keep would overwrite his file."""
    fix(project)
    calc = project / "src" / "calc.py"
    calc.write_text(calc.read_text(encoding="utf-8").replace("def mul", "# Nico was here\ndef mul"), encoding="utf-8")
    add_extra(project)
    folder = tree(project)
    assert refusal(lambda: keep_changes("req-2", project)) == (
        "conflict", conflict("src/calc.py was changed in your project"),
    )
    assert tree(project) == folder, "a refused Keep writes nothing"
    still = snapshot("req-2", repo=project)
    assert still["can_keep"] is True and still["can_discard"] is True and still["outcome"] == "review"
    assert discard_changes("req-2", repo=project)["discarded"] is True
    freed = snapshot("req-1", repo=project)
    assert freed["can_keep"] is True and freed["blocked_by"] is None
    assert refusal(lambda: keep_changes("req-1", project)) == (
        "conflict", conflict("src/calc.py was changed in your project"),
    ), "R1 changed the file he edited"
    assert tree(project) == folder and "# Nico was here" in calc.read_text(encoding="utf-8")


def test_keep_refuses_a_file_he_created_where_the_follow_up_adds_one(project):
    """HF3b: he creates src/extra.py, the very path R2 adds. R2's Keep is refused; R1 alone can still be kept."""
    fix(project)
    (project / "src" / "extra.py").write_text("EXTRA = 'Nico'\n", encoding="utf-8")
    add_extra(project)
    folder = tree(project)
    assert refusal(lambda: keep_changes("req-2", project)) == (
        "conflict", conflict("src/extra.py was added to your project"),
    )
    assert tree(project) == folder
    still = snapshot("req-2", repo=project)
    assert still["can_keep"] is True and still["outcome"] == "review"
    discard_changes("req-2", repo=project)
    kept = keep_changes("req-1", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == []
    assert (project / "src" / "extra.py").read_text(encoding="utf-8") == "EXTRA = 'Nico'\n"


# --- Check 3: a stack of three, and undoing the top ------------------------------------------------------------


def test_a_stack_of_three_hosted_requests_is_read_and_kept_together(project):
    """HF4: R3's agent sees R2's file and R1's fix; one Keep takes the three files and closes the other two."""
    fix(project)
    hand_edits(project)
    second = Script(reply(calls=[call("list", {"path": "src"}, "a")]), add_module("src/extra.py", "EXTRA = 1\n"),
                    reply("Added extra.py."))
    run(project, "req-2", "Now also add an extra module.", second)
    third = Script(reply(calls=[call("list", {"path": "src"}, "a"), call("read", {"path": "src/calc.py"}, "b")]),
                   add_module("src/third.py", "THIRD = 3\n"), reply("Added third.py."))
    at_first_call: dict = {}
    third.hook = lambda: at_first_call.update(record(project, "req-3"))
    review = run(project, "req-3", "And a third one.", third)
    assert "extra.py" in third.result("a") and FIXED in third.result("b"), "R3's agent sees R2's file and R1's fix"
    assert at_first_call["built_on"] == "req-2"
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py", "src/third.py"]
    assert baseline_of(project, "req-1") == baseline_of(project, "req-2") == baseline_of(project, "req-3")
    assert [snapshot(f"req-{n}", repo=project)["blocked_by"] for n in (1, 2)] == ["req-3", "req-3"]
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py", "src/third.py"]
    assert kept["also_kept"] == ["req-2", "req-1"]
    assert hand_edits_survive(project)
    assert history(project) == [("req-3", "kept"), ("req-2", "kept"), ("req-1", "kept")]


def test_discarding_the_top_of_three_hosted_requests_then_keeping_the_middle_one(project):
    fix(project)
    hand_edits(project)
    add_extra(project)
    add_third(project)
    assert discard_changes("req-3", repo=project)["discarded"] is True
    middle = snapshot("req-2", repo=project)
    assert middle["can_keep"] is True and middle["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["req-1"]
    assert not (project / "src" / "third.py").exists() and hand_edits_survive(project)
    assert history(project) == [("req-3", "discarded"), ("req-2", "kept"), ("req-1", "kept")]


# --- Check 4: nothing waits, or what waits is not whole: the agent starts from the folder, as before --------


def follow_up(project: Path, request_id: str = "req-2") -> SimpleNamespace:
    """A hosted follow-up that lists the folder, reads calc.py and adds src/extra.py."""
    script = Script(reply(calls=[call("list", {"path": "."}, "a"), call("read", {"path": "src/calc.py"}, "b")]),
                    add_module("src/extra.py", "EXTRA = 1\n"), reply("Added extra.py."))
    first: dict = {}
    script.hook = lambda: first.update(record(project, request_id))
    review = run(project, request_id, "Now also add an extra module.", script)
    return SimpleNamespace(script=script, first=first, review=review)


def test_the_first_hosted_request_ever_is_built_on_nothing(project):
    script = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "b")]), reply("Looked."))
    first: dict = {}
    script.hook = lambda: first.update(record(project, "req-1"))
    run(project, "req-1", "Look at add and tell me.", script)
    assert first["built_on"] is None
    assert "BUG" in script.result("b")


def test_a_hosted_request_after_a_keep_starts_from_the_folder_with_his_edits(project):
    fix(project)
    keep_changes("req-1", project)
    hand_edits(project)
    follow = follow_up(project)
    assert follow.first["built_on"] is None and "notes.txt" in follow.first["baseline_files"]
    assert "notes.txt" in follow.script.result("a") and FIXED in follow.script.result("b")
    assert follow.review["facts"]["files"] == ["src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/extra.py"] and kept["also_kept"] == [] and hand_edits_survive(project)


def test_a_hosted_request_after_a_discard_starts_from_the_folder_without_the_discarded_fix(project):
    fix(project)
    discard_changes("req-1", repo=project)
    hand_edits(project)
    follow = follow_up(project)
    assert follow.first["built_on"] is None and "notes.txt" in follow.first["baseline_files"]
    assert "notes.txt" in follow.script.result("a") and "BUG" in follow.script.result("b")
    assert follow.review["facts"]["files"] == ["src/extra.py"]


def lose_the_folder(older: Path) -> None:
    shutil.rmtree(older / "baseline")


def empty_the_folder(older: Path) -> None:
    shutil.rmtree(older / "baseline")
    (older / "baseline").mkdir()


def lose_a_file_from_the_folder(older: Path) -> None:
    (older / "baseline" / "tests" / "test_calc.py").unlink()


def lose_the_index(older: Path) -> None:
    (older / "baseline.json").unlink()


def corrupt_the_index(older: Path) -> None:
    (older / "baseline.json").write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("damage", [
    lose_the_folder, empty_the_folder, lose_a_file_from_the_folder, lose_the_index, corrupt_the_index,
], ids=["folder-gone", "folder-emptied", "folder-partly-gone", "index-gone", "index-corrupt"])
def test_a_waiting_result_whose_baseline_is_not_all_there_is_not_built_on_by_the_hosted_agent(project, damage):
    """An empty or partial baseline would list every file as added and refuse Keep for nothing: the agent starts
    from the folder as it is now instead, with no stack and no error."""
    fix(project)
    damage(request_dir(project_id_for(project), "req-1"))
    hand_edits(project)
    follow = follow_up(project)
    assert follow.first["built_on"] is None and "notes.txt" in follow.first["baseline_files"]
    assert "BUG" in follow.script.result("b"), "the agent started from the folder, not from the damaged result"
    assert follow.review["facts"]["files"] == ["src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/extra.py"] and kept["also_kept"] == [] and hand_edits_survive(project)


# --- Check 5: a running follow-up holds the older result back; a stopped, failed or idle one loses nothing ---


def test_a_follow_up_that_is_still_running_holds_the_older_request_back(project):
    """HF6: mid-run, the older result offers neither button, and a Keep or Discard of it is refused in plain words."""
    fix(project)
    folder = tree(project)
    mid: dict = {}

    def while_running() -> None:
        mid["snapshot"] = snapshot("req-1", repo=project)
        mid["keep"] = refusal(lambda: keep_changes("req-1", project))
        mid["discard"] = refusal(lambda: discard_changes("req-1", repo=project))
        mid["folder_untouched"] = tree(project) == folder

    script = Script(add_module("src/extra.py", "EXTRA = 1\n"), reply("Added extra.py."))
    script.hook = while_running
    run(project, "req-2", "Now also add an extra module.", script)
    during = mid["snapshot"]
    assert during["can_keep"] is False and during["can_discard"] is False
    assert during["blocked_by"] == "req-2" and during["summary"] == NEWER_RUNNING
    assert mid["keep"] == ("busy", NEWER_RUNNING) and mid["discard"] == ("busy", STILL_RUNNING)
    assert mid["folder_untouched"] is True and snapshot("req-1", repo=project)["outcome"] == "review"
    kept = keep_changes("req-2", project)
    assert kept["also_kept"] == ["req-1"] and kept["applied"] == ["src/calc.py", "src/extra.py"]


@pytest.mark.parametrize("how", ["stopped", "failed", "answered"])
def test_a_stopped_failed_or_idle_follow_up_has_no_result_of_its_own_and_loses_nothing(project, how):
    """HF7: R2 is stopped at its first call, its model fails, or it answers without a change. It lists no file and
    offers neither Keep nor Discard, so R1 is free at once, before any Discard. R1, its record and the folder are
    untouched; a Discard of R2 through the helper still goes through and changes nothing."""
    fix(project)
    pid = project_id_for(project)
    folder, r1_folder = tree(project), tree(request_dir(pid, "req-1"))
    cancel = threading.Event()
    if how == "stopped":
        script = Script(add_module("src/extra.py", "EXTRA = 1\n"), reply("Added extra.py."))
        script.hook = cancel.set
    elif how == "failed":
        script = Script(RuntimeError("the provider dropped the stream"))
    else:
        script = Script(reply("Nothing needed changing."))
    run(project, "req-2", "Now also add an extra module.", script, cancel_event=cancel)
    review = snapshot("req-2", repo=project)
    assert review["facts"]["files"] == [] and review["can_keep"] is False and review["can_discard"] is False
    assert review["blocked_by"] is None
    if how == "stopped":
        assert review["outcome"] == "stopped" and review["summary"] == "Stopped. Nothing was changed."
    elif how == "failed":
        assert review["outcome"] == "error" and "dropped the stream" in review["summary"]
    else:
        assert review["outcome"] == "review" and review["summary"] == "Nothing needed changing."
    freed = snapshot("req-1", repo=project)
    assert freed["can_keep"] is True and freed["can_discard"] is True and freed["blocked_by"] is None
    assert freed["outcome"] == "review"
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder
    assert discard_changes("req-2", repo=project)["discarded"] is True
    assert tree(project) == folder and tree(request_dir(pid, "req-1")) == r1_folder, "it changes nothing"
    kept = keep_changes("req-1", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == []


def test_a_plain_retry_after_a_failed_follow_up_builds_on_the_result_that_waits_and_one_keep_takes_both(project):
    """The natural next step after an error: send the request again. The failed R2 changed nothing, so R3 is built on
    R1 (it reads the fix), one Keep takes both, and R2 stays what it was: a request that failed, never `kept`."""
    fix(project)
    run(project, "req-2", "Now also document add().", Script(RuntimeError("the provider dropped the stream")))
    retry = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "a")]), document, reply("Documented."))
    run(project, "req-3", "Now also document add().", retry)
    assert load_meta(project_id_for(project), "req-3")["built_on"] == "req-1"
    assert FIXED in retry.result("a") and "BUG" not in retry.result("a"), "the retry reads the fix"
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]
    assert DOCUMENTED in (project / "src" / "calc.py").read_text(encoding="utf-8")
    assert history(project) == [("req-3", "kept"), ("req-2", "error"), ("req-1", "kept")]
    failed = snapshot("req-2", repo=project)
    assert failed["outcome"] == "error" and failed["summary"] != WENT_IN


# --- Check 6: the engine is chosen once and nothing falls back between models ---------------------------------


def test_the_engine_is_chosen_once_and_a_hosted_follow_up_builds_on_a_local_result(project, monkeypatch):
    """HF8: R1 on the local path (fake engine), R2 on the selected OrcaRouter model. Nothing local is touched."""
    machine.write_machine_config(machine.MachineConfig(
        model_alias="qwen/qwen3.8-27b", endpoint="https://api.orcarouter.ai", provider="orcarouter",
        api_key_name="ORCAROUTER_API_KEY", n_ctx=131072, max_tokens=16384, quantization="hosted",
    ))

    def never(*_args, **_kwargs):
        raise AssertionError("a hosted request must never start or call the local model")

    for name in ("ensure_model_server", "local_complete", "_apply_direct"):
        monkeypatch.setattr(coordinator, name, never)
    asked: list[bool] = []
    real = coordinator._agent_capable
    monkeypatch.setattr(coordinator, "_agent_capable", lambda: asked.append(True) or real())
    script = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "b")]), document, reply("Documented add()."))
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(record(project, "req-2"))
    monkeypatch.setattr(runner, "hosted_tool_chat", lambda _cfg, cancel=None: script)

    def local_fix(cwd: Path, _text: str, _cancel) -> None:
        calc = cwd / "src" / "calc.py"
        calc.write_text(calc.read_text(encoding="utf-8").replace(BUG, FIXED), encoding="utf-8")

    run_general_request(project, "Fix add so it adds instead of subtracting.", request_id="req-1",
                        fake_engine=local_fix, start_preview_after=False)
    review = run_general_request(project, "Now also document add().", request_id="req-2", start_preview_after=False)
    first, second = load_meta(project_id_for(project), "req-1"), load_meta(project_id_for(project), "req-2")
    assert asked == [True], "the engine choice is asked once, for the workspace and the run together"
    assert (first["engine"], second["engine"], review["engine"]) == ("local", "agent", "agent")
    assert second["model_id"] == "qwen/qwen3.8-27b" and second["provider"] == "orcarouter"
    assert at_first_call["built_on"] == "req-1" and FIXED in script.result("b") and "BUG" not in script.result("b")
    assert keep_changes("req-2", project)["also_kept"] == ["req-1"]


# --- A continuation never inherits a baseline, the hosted agent included ---------------------------------------


def test_a_hosted_continuation_brings_its_own_copy_and_inherits_no_baseline(project):
    """`continue_from` is untouched by this cut: it is built on the request it continues, but its baseline is the
    folder as it is now (his edits included), and the agent starts in the continued request's result."""
    fix(project)
    hand_edits(project)
    script = Script(reply(calls=[call("read", {"path": "src/calc.py"}, "a")]),
                    add_module("src/extra.py", "EXTRA = 1\n"), reply("Carried on."))
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(record(project, "req-2"))
    review = run(project, "req-2", "Carry on", script, continue_from="req-1")
    assert at_first_call["built_on"] == "req-1" and "notes.txt" in at_first_call["baseline_files"]
    assert FIXED in script.result("a") and "BUG" not in script.result("a")
    assert {"src/calc.py", "src/extra.py"} <= set(review["facts"]["files"])


def test_a_question_while_the_stack_waits_changes_nothing_and_is_told_the_newest_request(project, monkeypatch):
    """The question path reads the waiting copy; it must not write anything in the folder or in any request."""
    fix(project)
    add_extra(project)
    pid = project_id_for(project)
    before = (tree(project), tree(request_dir(pid, "req-1")), tree(request_dir(pid, "req-2")))
    machine.write_machine_config(machine.MachineConfig(
        model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b", endpoint="https://integrate.api.nvidia.com",
        provider="nvidia", api_key_name="NVIDIA_API_KEY", n_ctx=131072, max_tokens=16384, quantization="hosted",
    ))
    script = Script(reply("Okay."))
    monkeypatch.setattr(
        runner, "hosted_chat",
        lambda cfg, body, *, thinking, **_: script(body["messages"], body.get("tools") or []),
    )
    chat_request(project, "Did the fix work? What does add(2, 3) return now?")
    note = str(script.sent[0][-1]["content"])
    assert "src/calc.py" in note and "src/extra.py" in note, "it is told about the newest request and the stack's files"
    assert (tree(project), tree(request_dir(pid, "req-1")), tree(request_dir(pid, "req-2"))) == before
