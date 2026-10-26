"""Cut starter-in-candidate: an empty folder changes only when Nico presses Keep.

Before, a request on an empty folder wrote the Vite/React starter into the folder before the model had written a
line. It stayed there after Discard, after Stop and after a failed run, the review never listed it and Keep never
wrote it. Now the starter is part of the request's working copy (`live/candidate`) and not of its baseline: the
review lists its files as the request's own new files, Keep writes them and Discard leaves the folder exactly as it
was. Three things have to follow it:

* the hosted agent starts in the working copy as the request started (the starter), not in the empty baseline;
* a working copy that is still exactly the starter is no result: no files, no Keep or Discard, no check and no
  repair round;
* a request built on a waiting starter result inherits that result's baseline and copy and never lays the starter again.

A scripted hosted model (`agent_chat`) and fake engines stand in for the models ($0); the workspace, baselines, Keep,
Discard, History and the agent's tools are the real code. Every test is modelled on a scenario of the cut's probe
(SC: a gap the cut closes, SG: a guard that must not move) and names the acceptance check it belongs to.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from local_coder.app_service import discard_changes, keep_changes, snapshot
from local_coder.general import starter as starter_module
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
from local_coder.general.waiting import find_waiting_result, waiting_note

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="the agent's bash runs in sandbox-exec: macOS only")

OLD_APP = "<h1>New app</h1>\n      <p>This starter is empty on purpose. Describe what you want to build.</p>"
STARTER_FILES = sorted(index_tree(starter_root()).files)
WANT = sorted({*STARTER_FILES, "src/Tasks.tsx"})
NO_CHANGES = "No file changes were recorded. The original project was not modified."
STOPPED_NOTHING = "Stopped. Nothing was changed."
STOPPED_FILES = "Stopped. Review what changed so far, or discard it."
UNDONE = "A later request undid these changes, so they were not added to your project."
NOTHING_LEFT = "This result has no files left, so Keep would empty your project. Your project was not changed."
NOTHING_TO_KEEP = "This request changed nothing, so there is nothing to keep."


def conflict(what: str) -> str:
    """Nico's sentence for a refused Keep: which file, and what happened to it."""
    return f"Not kept: since these changes were started, {what}. Your project was not changed."


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME with the data dir inside it, so the hosted agent may read and write as it does in the app."""
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(root / ".local" / "share" / "local-coder"))
    return root


@pytest.fixture
def folder(home) -> Path:
    """An existing, empty project folder, as Finder or `mkdir` leaves it."""
    path = home / "projects" / "board"
    path.mkdir(parents=True)
    return path


# --- Fake engines, a scripted hosted model and the things the scenarios look at -------------------------------


def entries(root: Path) -> list[str]:
    """Everything under root, hidden files and folders included: 'the folder is empty' means this list is empty."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def files_in(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def digest(root: Path) -> dict[str, bytes | None]:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def tasks(cwd: Path) -> None:
    """The model replaces the starter's text and adds a file."""
    app = cwd / "src" / "App.tsx"
    app.write_text(app.read_text(encoding="utf-8").replace(OLD_APP, "<h1>Tasks</h1>"), encoding="utf-8")
    (cwd / "src" / "Tasks.tsx").write_text("export const tasks = ['milk'];\n", encoding="utf-8")


def other(cwd: Path) -> None:
    (cwd / "src" / "Other.tsx").write_text("export const other = 1;\n", encoding="utf-8")


def extra(cwd: Path) -> None:
    (cwd / "src" / "extra.ts").write_text("export const extra = 1;\n", encoding="utf-8")


def nothing(_cwd: Path) -> None:
    return None


def wipe(cwd: Path) -> None:
    for item in sorted(cwd.iterdir()):
        if item.name != "node_modules":
            shutil.rmtree(item) if item.is_dir() else item.unlink()


def back_to_the_blank_starter(cwd: Path) -> None:
    """What an 'undo that' follow-up leaves if the model restores the starter byte for byte."""
    wipe(cwd)
    shutil.copytree(starter_root(), cwd, dirs_exist_ok=True)


def local(project: Path, request_id: str, engine, **kwargs) -> dict:
    """A request on the local path with a fake engine."""
    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=lambda cwd, _text, _cancel: engine(cwd), **kwargs,
    )


def local_then_stop(project: Path, request_id: str, engine) -> dict:
    """A fake engine that does its work and then the user presses Stop."""
    def stopping(cwd: Path, _text: str, cancel: threading.Event) -> None:
        engine(cwd)
        cancel.set()

    return run_general_request(
        project, f"request {request_id}", request_id=request_id, start_preview_after=False,
        fake_engine=stopping, cancel_event=threading.Event(),
    )


def call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A scripted hosted model: it answers with the replies in order and keeps what it was sent."""

    def __init__(self, *replies: dict) -> None:
        self.replies, self.sent = list(replies), []

    def __call__(self, messages: list[dict], _tools: list[dict]) -> dict:
        self.sent.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0)

    def tool_results(self, step: int) -> list[str]:
        """What the tools told the model at its call number `step` (0 is the first call)."""
        if len(self.sent) <= step:
            return []
        return [str(m.get("content", "")) for m in self.sent[step] if m.get("role") == "tool"]


def hosted(project: Path, request_id: str, script: Script, text: str = "Show Tasks", **kwargs) -> dict:
    return run_general_request(
        project, text, request_id=request_id, agent_chat=script, start_preview_after=False, **kwargs,
    )


def agent_tasks() -> Script:
    """The hosted agent reads the starter, edits its text and writes a file."""
    return Script(
        reply(calls=[call("read", {"path": "package.json"}, "1"), call("read", {"path": "src/App.tsx"}, "2")]),
        reply(calls=[call("edit", {"path": "src/App.tsx", "old_string": OLD_APP, "new_string": "<h1>Tasks</h1>"}, "3"),
                     call("write", {"path": "src/Tasks.tsx", "content": "export const tasks = ['milk'];\n"}, "4")]),
        reply("I made the page show Tasks."),
    )


def history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def refusal(action) -> tuple[str, str] | None:
    """(code, sentence) an action was refused with, or None when it went through."""
    try:
        action()
    except GeneralError as exc:
        return exc.code, exc.detail
    return None


def working_copy(project: Path) -> Path:
    return live_dir(project_id_for(project)) / "candidate"


# === Check 1: a first request on an existing empty folder, local engine (SC1) ===================================


def test_a_first_request_on_an_empty_folder_builds_the_starter_in_its_working_copy(folder):
    review = local(folder, "r1", tasks)
    assert entries(folder) == [], "nothing is written into his folder before Keep, hidden files included"
    assert review["facts"]["files"] == WANT, "the review lists the starter as the request's own files"
    delta = review["facts"]["delta"]
    assert delta["modified"] == [] and sorted(delta["added"]) == WANT
    assert "+++ b/package.json" in review["diff"] and "+++ b/src/App.tsx" in review["diff"]
    assert "<h1>Tasks</h1>" in review["diff"] and "<h1>New app</h1>" not in review["diff"]
    assert review["can_keep"] is True and review["can_discard"] is True
    meta = load_meta(review["project_id"], "r1")
    assert meta["starter_used"] is True
    assert index_tree(Path(meta["baseline"])).files == {}, "the baseline is the folder as it was: empty"
    kept = keep_changes("r1", folder)
    assert kept["ok"] is True and sorted(kept["applied"]) == WANT
    assert files_in(folder) == WANT and "<h1>Tasks</h1>" in (folder / "src" / "App.tsx").read_text(encoding="utf-8")
    assert not (folder / "node_modules").exists() and not (folder / "package-lock.json").exists()
    assert sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) < 20_000, "Keep stays small"


# === Check 2: the same through the hosted agent (SC2) ===========================================================


def test_the_hosted_agent_starts_in_the_starter_and_the_folder_stays_empty_until_keep(folder):
    script = agent_tasks()
    review = hosted(folder, "r1", script)
    assert any('"name": "local-coder-app"' in text for text in script.tool_results(1)), (
        "the agent's first step did not empty its working copy: it read the starter"
    )
    assert review["engine"] == "agent" and "src/App.tsx" in review["facts"]["files"], "its edit of the starter worked"
    assert entries(folder) == []
    assert review["facts"]["files"] == WANT
    assert load_meta(review["project_id"], "r1")["stop_reason"] == "checks_passed"
    kept = keep_changes("r1", folder)
    assert sorted(kept["applied"]) == WANT and files_in(folder) == WANT


# === Check 3: Discard leaves the folder empty and carries nothing forward (SC3) =================================


def test_discard_leaves_the_folder_empty_and_the_next_request_starts_from_the_starter_again(folder):
    pid = project_id_for(folder)
    local(folder, "r1", tasks)
    assert discard_changes("r1", repo=folder)["discarded"] is True
    assert entries(folder) == [], "not even a src/ folder"
    assert history(folder) == [("r1", "discarded")]
    assert index_tree(working_copy(folder)).files == {}, "the sticky working copy lists no source file"
    second = local(folder, "r2", other)
    assert second["facts"]["files"] == sorted({*STARTER_FILES, "src/Other.tsx"})
    assert not load_meta(pid, "r2").get("built_on")
    candidate_app = (request_dir(pid, "r2") / "candidate" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert OLD_APP in candidate_app, "the pristine starter, not the discarded request's edit"
    kept = keep_changes("r2", folder)
    assert sorted(kept["applied"]) == sorted({*STARTER_FILES, "src/Other.tsx"})
    assert not (folder / "src" / "Tasks.tsx").exists(), "the folder has no trace of the discarded request"


def test_the_hosted_agent_after_a_discard_still_starts_in_the_starter(home):
    folder = home / "projects" / "again"
    folder.mkdir(parents=True)
    local(folder, "r1", tasks)
    discard_changes("r1", repo=folder)
    script = agent_tasks()
    review = hosted(folder, "r2", script)
    assert any('"name": "local-coder-app"' in text for text in script.tool_results(1))
    assert entries(folder) == [] and review["facts"]["files"] == WANT


# === Check 4: a blank starter is never a result (SC4, SC5) ======================================================


def test_a_first_request_that_changes_nothing_is_no_result_and_a_keep_that_arrives_anyway_is_refused(folder):
    review = local(folder, "r1", nothing)
    assert entries(folder) == []
    assert review["facts"]["files"] == [] and review["can_keep"] is False and review["can_discard"] is False
    assert review["summary"] == NO_CHANGES
    assert refusal(lambda: keep_changes("r1", folder)) == ("nothing_to_keep", NOTHING_TO_KEEP)
    assert entries(folder) == []


def test_a_hosted_agent_that_only_answers_costs_one_model_call_and_the_next_request_starts_from_the_folder(folder):
    pid = project_id_for(folder)
    script = Script(reply("There is nothing to change in an empty project."))
    review = hosted(folder, "r1", script, text="Make it nicer")
    # One answer, sent back once to check first (look_first): no check and no repair round on a blank starter.
    assert len(script.sent) == 2
    assert review["facts"]["files"] == [] and review["can_keep"] is False and review["can_discard"] is False
    assert review["verification"]["preview"] == "not_run"
    assert entries(folder) == []
    second = local(folder, "r2", tasks)
    assert not load_meta(pid, "r2").get("built_on"), "the idle request is not something to build on"
    assert second["facts"]["files"] == WANT


def test_stop_before_the_first_write_says_nothing_was_changed(folder):
    review = local_then_stop(folder, "r1", nothing)
    assert review["summary"] == STOPPED_NOTHING
    assert review["facts"]["files"] == [] and review["can_keep"] is False
    assert entries(folder) == []


def test_stop_after_a_write_keeps_what_was_written_including_the_starter(folder):
    review = local_then_stop(folder, "r1", tasks)
    assert review["summary"] == STOPPED_FILES and review["can_keep"] is True
    assert review["facts"]["files"] == WANT
    assert entries(folder) == []


# === Check 5: stacking on a starter result (SC6) ================================================================


def test_a_follow_up_on_a_starter_result_does_not_lay_the_starter_again_and_one_keep_takes_both(folder):
    pid = project_id_for(folder)
    local(folder, "r1", tasks)
    review = local(folder, "r2", extra)
    meta = load_meta(pid, "r2")
    stack = sorted({*WANT, "src/extra.ts"})
    assert meta["built_on"] == "r1" and meta["starter_used"] is False
    assert index_tree(Path(meta["baseline"])).files == {}, "it inherits the empty baseline of the one it is built on"
    assert review["facts"]["files"] == stack
    assert "<h1>Tasks</h1>" in (request_dir(pid, "r2") / "candidate" / "src" / "App.tsx").read_text(encoding="utf-8"), (
        "the first request's edit survives: the starter was not laid again over it"
    )
    assert entries(folder) == []
    older = snapshot("r1", repo=folder)
    assert older["can_keep"] is False and older["blocked_by"] == "r2"
    kept = keep_changes("r2", folder)
    assert sorted(kept["applied"]) == stack and kept["also_kept"] == ["r1"]
    assert history(folder) == [("r2", "kept"), ("r1", "kept")]
    assert files_in(folder) == stack


def test_a_hosted_follow_up_on_a_starter_result_starts_from_that_result(folder):
    pid = project_id_for(folder)
    local(folder, "r1", tasks)
    script = Script(
        reply(calls=[call("read", {"path": "src/App.tsx"}, "1")]),
        reply(calls=[call("write", {"path": "src/extra.ts", "content": "export const extra = 1;\n"}, "2")]),
        reply("Added extra.ts."),
    )
    review = hosted(folder, "r2", script, text="Add extra")
    assert any("<h1>Tasks</h1>" in text for text in script.tool_results(1)), "the agent read the first request's edit"
    assert load_meta(pid, "r2")["built_on"] == "r1"
    assert review["facts"]["files"] == sorted({*WANT, "src/extra.ts"})
    assert entries(folder) == []


# === Check 6: back to the empty folder, and net-out's guard (SC7) ===============================================


def test_a_follow_up_that_wipes_the_working_copy_ends_at_the_empty_folder_without_net_outs_sentence(folder):
    local(folder, "r1", tasks)
    review = local(folder, "r2", wipe)
    assert review["facts"]["files"] == [] and review["can_keep"] is False
    assert review["summary"] != NOTHING_LEFT, "the baseline is empty, so net-out's guard has nothing to say"
    assert entries(folder) == []


def test_a_first_request_that_deletes_everything_it_was_given_has_no_files(home):
    folder = home / "projects" / "wiped"
    folder.mkdir(parents=True)
    review = local(folder, "r1", wipe)
    assert review["facts"]["files"] == [] and review["can_keep"] is False
    assert review["summary"] != NOTHING_LEFT
    assert entries(folder) == []


@darwin_only
def test_the_hosted_agents_real_bash_undoing_the_starter_closes_the_first_request_as_undone(folder):
    local(folder, "r1", tasks)
    remove = ("rm -rf src index.html package.json tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts "
              ".gitignore; ls -A")
    script = Script(reply(calls=[call("bash", {"command": remove, "description": "remove it"}, "1")]),
                    reply("Removed everything."))
    review = hosted(folder, "r2", script, text="Remove everything again")
    assert review["facts"]["files"] == [] and review["can_keep"] is False
    closed = snapshot("r1", repo=folder)
    assert closed["outcome"] == "discarded" and closed["summary"] == UNDONE
    assert refusal(lambda: keep_changes("r1", folder)) == ("discarded", UNDONE)
    assert entries(folder) == []


# === Check 7: his own files while a result waits (SC8) ==========================================================


def test_a_new_file_of_his_is_left_alone_when_the_starter_result_is_kept(folder):
    local(folder, "r1", tasks)
    (folder / "notes.txt").write_text("mine\n", encoding="utf-8")
    kept = keep_changes("r1", folder)
    assert sorted(kept["applied"]) == WANT, "notes.txt is not part of the result"
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "mine\n"


def test_his_own_package_json_is_a_named_clash_and_keep_writes_nothing(folder):
    local(folder, "r1", tasks)
    (folder / "package.json").write_text('{"name": "mine"}\n', encoding="utf-8")
    before = digest(folder)
    refused = refusal(lambda: keep_changes("r1", folder))
    assert refused == ("conflict", conflict("package.json was added to your project"))
    assert digest(folder) == before and files_in(folder) == ["package.json"], "his file is the only one in the folder"


# === Check 9: a question while the starter result waits (SC11) ==================================================


def test_a_question_while_the_starter_result_waits_is_told_the_files_are_not_in_the_folder_yet(folder):
    local(folder, "r1", tasks)
    waiting = find_waiting_result(folder)
    assert waiting is not None and "package.json" in waiting.files
    plain, agent = waiting_note(folder, can_read=False), waiting_note(folder, can_read=True)
    assert "not in the folder yet" in plain and "not in the folder yet" in agent
    assert "package.json" in plain and plain.rstrip(".").endswith("more)")
    assert len(plain) < 700 and len(agent) < 1100, "the note stays short"
    assert str(waiting.candidate) in agent, "the agent is told where to read the waiting copy"
    assert entries(folder) == []


# === Check 11: the preview runs from the request's copy, not from his folder ====================================


def test_the_preview_of_a_first_request_is_started_in_its_candidate_folder_never_in_his_folder(folder, monkeypatch):
    started: list[str] = []

    def fake_start(project_dir, project_id, **_kwargs):
        started.append(str(project_dir))
        return {"ok": True, "url": "http://127.0.0.1:4199/", "dir": str(project_dir), "project_id": project_id}

    monkeypatch.setattr("local_coder.general.coordinator.start_preview", fake_start)
    monkeypatch.setattr("local_coder.general.coordinator.check_preview_health",
                        lambda _url, _dir=None, **_kw: {"ok": True, "code": "ok", "detail": "fake"})
    review = run_general_request(folder, "Show Tasks", request_id="r1", start_preview_after=True,
                                 fake_engine=lambda cwd, _text, _cancel: tasks(cwd))
    pid = project_id_for(folder)
    assert started == [str(request_dir(pid, "r1") / "candidate")], "the preview runs from the request's own copy"
    assert not str(started[0]).startswith(str(folder))
    assert entries(folder) == []
    assert review["facts"]["files"] == WANT
    assert not any(name.startswith("node_modules") or name == "package-lock.json" for name in review["facts"]["files"])


# === The reported scenario: a follow-up that ends exactly at the blank starter is still a result ================


def test_a_follow_up_that_ends_exactly_at_the_blank_starter_is_still_a_result_only_the_old_label_warns(folder):
    """Reported, not changed here: only a first request on an empty folder is 'idle' when it stays at the starter.
    A follow-up built on a waiting result that restores the starter byte for byte lists the starter's files as
    its own, offers Keep and holds the older request back; the old 'empty starter page' check is what warns."""
    local(folder, "r1", tasks)
    review = local(folder, "r2", back_to_the_blank_starter)
    assert review["facts"]["files"] == STARTER_FILES
    assert review["can_keep"] is True and review["can_discard"] is True
    assert snapshot("r1", repo=folder)["blocked_by"] == "r2"
    assert "empty starter page" in review["summary"] and review["verification"]["preview"] == "failed"
    assert entries(folder) == []


# === Check 12: what must not move (SG1, SG5, and requests an older build left waiting) ==========================


def test_an_existing_project_with_files_never_gets_a_starter(home):
    project = home / "projects" / "calc"
    shutil.copytree(Path(__file__).parent.parent / "fixtures" / "demo_calc" / "repo", project)
    before = digest(project)

    def fix(cwd: Path) -> None:
        calc = cwd / "src" / "calc.py"
        calc.write_text(calc.read_text(encoding="utf-8").replace("a - b  # BUG: subtracts instead of adding", "a + b"),
                        encoding="utf-8")

    review = local(project, "r1", fix)
    assert digest(project) == before
    assert load_meta(review["project_id"], "r1")["starter_used"] is False
    assert review["facts"]["files"] == ["src/calc.py"]
    kept = keep_changes("r1", project)
    assert kept["applied"] == ["src/calc.py"]
    assert "package.json" not in files_in(project) and "src/App.tsx" not in files_in(project)


def test_a_starter_missing_from_the_install_is_a_loud_error_before_any_model_call(folder, monkeypatch, tmp_path):
    monkeypatch.setattr(starter_module, "starter_root", lambda: tmp_path / "no-such-starter")
    with pytest.raises(FileNotFoundError):
        local(folder, "r1", tasks)
    assert entries(folder) == [], "and not an empty working copy that the model then fills"


def test_requests_an_older_build_left_waiting_with_the_starter_in_their_baseline_read_stack_keep_and_discard(home):
    """SG6: the installed build wrote the starter into the folder, so its baseline holds it and the folder has it."""
    old = home / "projects" / "legacy"
    shutil.copytree(starter_root(), old)
    first = local(old, "r1", tasks)
    meta = load_meta(first["project_id"], "r1")
    meta["starter_used"] = True  # what the old build recorded for a folder it had just filled
    record = request_dir(first["project_id"], "r1") / "meta.json"
    record.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    assert snapshot("r1", repo=old)["facts"]["files"] == ["src/App.tsx", "src/Tasks.tsx"]
    second = local(old, "r2", extra)
    assert load_meta(second["project_id"], "r2")["built_on"] == "r1"
    assert load_meta(second["project_id"], "r2")["starter_used"] is False
    assert second["facts"]["files"] == ["src/App.tsx", "src/Tasks.tsx", "src/extra.ts"]
    kept = keep_changes("r2", old)
    assert kept["applied"] == ["src/App.tsx", "src/Tasks.tsx", "src/extra.ts"] and kept["also_kept"] == ["r1"]
    discarded = home / "projects" / "legacy-discard"
    shutil.copytree(starter_root(), discarded)
    local(discarded, "r1", tasks)
    assert discard_changes("r1", repo=discarded)["discarded"] is True
    assert (discarded / "package.json").is_file(), "the starter the old build wrote stays"
    again = local(discarded, "r2", other)
    assert load_meta(again["project_id"], "r2")["starter_used"] is False
    assert again["facts"]["files"] == ["src/Other.tsx"], "a project, not an empty folder: no starter again"
