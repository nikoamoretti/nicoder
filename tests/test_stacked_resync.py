"""Cut stacked-resync: Keep never reverts what Nico changed in the folder himself.

A request built on a result that still waits (it runs on the project's sticky working copy, whichever engine:
the local model or the hosted agent) starts from that result, not from the folder. Its baseline has to be that
result's baseline, or the diff and Keep would measure from two different starting points, and whatever Nico
changed in the folder meanwhile would show up as the model's work and be reverted by Keep. A continuation
(`continue_from`) is the exception: it brings its own copy and never inherits a baseline.

Every scenario here edits the folder by hand while a result waits.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_service import discard_changes, keep_changes, snapshot
from local_coder.general import coordinator
from local_coder.general.coordinator import GeneralError, list_requests, load_meta, request_dir, run_general_request

OLD = "def add(a, b):\n    return a - b\n"
NEW = "def add(a, b):\n    return a + b\n"
README = "# calc\n"
HIS_README = "# calc\nNico's own paragraph\n"
HIS_NOTES = "Nico's notes\n"
HIS_CALC = "def add(a, b):\n    return 0  # Nico's own version\n"
CONFLICT = "Some files changed in the project while this request was running."
WENT_IN = "These changes went in with a later request you kept."
DECIDE_NEWER_FIRST = "A newer request in this project builds on this one, so Keep or Discard the newer one first."
MODEL = "Qwen3.5-4B-Q4_K_M"

HOSTED = machine.MachineConfig(
    model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b",
    endpoint="https://integrate.api.nvidia.com",
    provider="nvidia",
    api_key_name="NVIDIA_API_KEY",
    n_ctx=131072,
    max_tokens=16384,
    quantization="hosted",
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(root / ".local" / "share" / "local-coder"))
    return root


@pytest.fixture
def project(home) -> Path:
    root = home / "projects" / "calc"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text(OLD, encoding="utf-8")
    (root / "src" / "util.py").write_text("UTIL = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "calc"\n', encoding="utf-8")
    (root / "README.md").write_text(README, encoding="utf-8")
    return root


def _writes(relative: str, text: str):
    def engine(cwd: Path, _text: str, _cancel) -> None:
        (cwd / relative).parent.mkdir(parents=True, exist_ok=True)
        (cwd / relative).write_text(text, encoding="utf-8")

    return engine


def _deletes(relative: str):
    def engine(cwd: Path, _text: str, _cancel) -> None:
        (cwd / relative).unlink()

    return engine


def _run(project: Path, request_id: str, engine) -> dict:
    return run_general_request(project, f"request {request_id}", request_id=request_id, fake_engine=engine,
                               start_preview_after=False)


def _fix(project: Path, request_id: str = "req-1") -> dict:
    return _run(project, request_id, _writes("src/calc.py", NEW))


def _extra(project: Path, request_id: str = "req-2") -> dict:
    return _run(project, request_id, _writes("src/extra.py", "EXTRA = 1\n"))


def _third(project: Path, request_id: str = "req-3") -> dict:
    return _run(project, request_id, _writes("src/third.py", "THIRD = 3\n"))


def _hand_edits(project: Path) -> None:
    """What Nico does in the folder while a result waits: edits a file and adds one."""
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    (project / "notes.txt").write_text(HIS_NOTES, encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def _baseline(project: Path, request_id: str) -> tuple[bytes, dict[str, bytes]]:
    """A request's starting point on disk: its frozen index and its baseline folder."""
    root = request_dir(coordinator.project_id_for(project), request_id)
    return (root / "baseline.json").read_bytes(), _tree(root / "baseline")


def _record(project: Path, request_id: str) -> dict:
    """What the request's record says right now: its link and the files its baseline lists."""
    pid = coordinator.project_id_for(project)
    frozen = json.loads((request_dir(pid, request_id) / "baseline.json").read_text(encoding="utf-8"))["files"]
    return {"built_on": load_meta(pid, request_id).get("built_on"), "baseline_files": sorted(frozen)}


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class _Script:
    """A scripted hosted model: replies in order, remembers what it was sent, runs a hook at its first call."""

    def __init__(self, *replies: dict) -> None:
        self.replies, self.sent, self.hook = list(replies), [], None

    def __call__(self, messages: list[dict], _tools: list[dict]) -> dict:
        if not self.sent and self.hook:
            self.hook()  # the request is still being made: its record is what the agent starts from
        self.sent.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0)

    def tool_text(self) -> str:
        """What the agent's tools told it, as the model saw it at its last call."""
        last = self.sent[-1] if self.sent else []
        return "\n".join(str(message.get("content")) for message in last if message.get("role") == "tool")


def _agent_fix(project: Path, request_id: str = "req-1") -> dict:
    """A hosted-agent run that fixes add(). Nothing waits in these tests when it runs, so it starts from the folder."""
    script = _Script(
        _reply(calls=[_call("write", {"path": "src/calc.py", "content": NEW}, "w")]), _reply("Fixed add()."),
    )
    return run_general_request(project, f"request {request_id}", request_id=request_id, agent_chat=script,
                               start_preview_after=False)


# --- The hosted agent builds on a waiting result like every other engine ------------------------------------


def test_a_hosted_follow_up_builds_on_the_waiting_result_and_his_edits_survive(project):
    """H1: the follow-up works on another file. It reads the waiting fix, its review is the stack's, and one Keep
    takes both. The known limit of building on the result: it does not see what he edited after R1 started."""
    _agent_fix(project)
    _hand_edits(project)
    script = _Script(
        _reply(calls=[_call("list", {"path": "."}, "a"), _call("read", {"path": "src/calc.py"}, "b")]),
        _reply(calls=[_call("write", {"path": "src/extra.py", "content": "EXTRA = 1\n"}, "c")]),
        _reply("Added extra.py."),
    )
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(_record(project, "req-2"))
    second = run_general_request(project, "request req-2", request_id="req-2", agent_chat=script,
                                 start_preview_after=False)
    seen = script.tool_text()
    assert "return a + b" in seen and "return a - b" not in seen, "it reads the fix that still waits"
    assert "notes.txt" not in seen, "his later edits are not in the copy it starts from (the known limit)"
    assert at_first_call["built_on"] == "req-1"
    assert "notes.txt" not in at_first_call["baseline_files"], "R1's baseline: his edits are not the model's work"
    assert second["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["blocked_by"] == "req-2", "R1 is decided with the newer request"
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["req-1"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert (project / "notes.txt").read_text(encoding="utf-8") == HIS_NOTES
    assert _history(project) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_hosted_follow_up_on_the_same_file_sees_the_waiting_fix_and_one_keep_takes_both(project):
    """H2: the same-file follow-up edits the fixed line, so the newer Keep closes the older request; no Keep is
    offered that can only fail."""
    _agent_fix(project)
    script = _Script(
        _reply(calls=[_call("read", {"path": "src/calc.py"}, "a")]),
        _reply(calls=[_call("write", {"path": "src/calc.py", "content": NEW + "# documented\n"}, "b")]),
        _reply("Documented add()."),
    )
    second = run_general_request(project, "request req-2", request_id="req-2", agent_chat=script,
                                 start_preview_after=False)
    seen = script.tool_text()
    assert "return a + b" in seen and "return a - b" not in seen, "it reads the fix that still waits"
    assert second["facts"]["files"] == ["src/calc.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == ["req-1"]
    before = _tree(project)
    with pytest.raises(GeneralError) as closed:
        keep_changes("req-1", project)
    assert closed.value.code == "kept", "the older request is closed, not refused as a conflict"
    assert _tree(project) == before
    assert _history(project) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_hosted_run_is_built_on_a_waiting_result_before_its_first_model_call(project, monkeypatch):
    """The link and the inherited baseline are on disk when the workspace is made, so the older request is held back
    from that moment and the working copy the agent starts in already holds the waiting result."""
    _fix(project)
    _hand_edits(project)
    made: dict = {}
    real = coordinator.prepare_workspace

    def watch(*args, **kwargs):
        meta = real(*args, **kwargs)
        made["in_memory"] = meta.get("built_on")
        made["on_disk"] = load_meta(meta["project_id"], meta["request_id"]).get("built_on")
        made["baseline_files"] = _record(project, meta["request_id"])["baseline_files"]
        return meta

    monkeypatch.setattr(coordinator, "prepare_workspace", watch)
    during: dict = {}
    script = _Script(_reply(calls=[_call("write", {"path": "src/extra.py", "content": "EXTRA = 1\n"}, "a")]),
                     _reply("Added extra.py."))
    working_copy = coordinator.live_dir(coordinator.project_id_for(project)) / "candidate"
    script.hook = lambda: during.update(
        snapshot("req-1", repo=project), copy=(working_copy / "src" / "calc.py").read_text(encoding="utf-8"),
    )
    run_general_request(project, "request req-2", request_id="req-2", agent_chat=script, start_preview_after=False)
    assert made["in_memory"] == "req-1" and made["on_disk"] == "req-1"
    assert "notes.txt" not in made["baseline_files"], "the baseline is the waiting result's, inherited whole"
    assert during["can_keep"] is False and during["blocked_by"] == "req-2", "the older request is held back"
    assert during["copy"] == NEW, "the agent starts in the working copy as the waiting result left it"


def test_the_engine_is_decided_once_for_the_workspace_and_the_run(project, monkeypatch):
    """With a hosted model selected the request reads the engine choice one time; its workspace and its run agree:
    both build on the waiting result."""
    from local_coder.agent import runner

    machine.write_machine_config(HOSTED)
    _fix(project)
    _hand_edits(project)
    asked: list[bool] = []
    real = coordinator._agent_capable
    monkeypatch.setattr(coordinator, "_agent_capable", lambda: asked.append(True) or real())
    script = _Script(
        _reply(calls=[_call("read", {"path": "src/calc.py"}, "r"),
                      _call("write", {"path": "src/extra.py", "content": "EXTRA = 1\n"}, "a")]),
        _reply("Added extra.py."),
    )
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(_record(project, "req-2"))
    monkeypatch.setattr(runner, "hosted_tool_chat", lambda _cfg, cancel=None: script)
    review = run_general_request(project, "request req-2", request_id="req-2", start_preview_after=False)
    assert asked == [True], "one decision, not one for the workspace and another for the run"
    assert review["engine"] == "agent"
    assert at_first_call["built_on"] == "req-1" and "notes.txt" not in at_first_call["baseline_files"]
    assert "return a + b" in script.tool_text(), "the agent read the waiting fix"
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    assert keep_changes("req-2", project)["applied"] == ["src/calc.py", "src/extra.py"]


# --- His hand edits survive a stacked Keep (the High) ---------------------------------------------------------


def test_his_hand_edits_survive_keeping_a_stacked_request(project):
    """V1: R1 waits, he edits README.md and adds notes.txt, R2 builds on R1. Keep R2 puts in the two requests' files."""
    _fix(project)
    _hand_edits(project)
    _extra(project)
    review = snapshot("req-2", repo=project)
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py"], "his edits are not the model's work"
    assert review["facts"]["delta"]["deleted"] == [] and review["facts"]["delta"]["added"] == ["src/extra.py"]
    assert "README.md" not in review["diff"] and "notes.txt" not in review["diff"]
    assert _baseline(project, "req-2") == _baseline(project, "req-1"), "one starting point for the whole stack"
    kept = keep_changes("req-2", project)
    assert kept["ok"] is True and kept["applied"] == ["src/calc.py", "src/extra.py"]
    assert kept["also_kept"] == ["req-1"]
    assert (project / "src" / "calc.py").read_text(encoding="utf-8") == NEW
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert (project / "notes.txt").read_text(encoding="utf-8") == HIS_NOTES
    assert _history(project) == [("req-2", "kept"), ("req-1", "kept")]
    older = snapshot("req-1", repo=project)
    assert older["summary"] == WENT_IN and older["kept_with"] == "req-2"


@pytest.fixture
def web_project(home) -> Path:
    """A small TypeScript project: the file-block apply of the local model path only takes web sources."""
    root = home / "projects" / "web"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"p"}\n', encoding="utf-8")
    (root / "README.md").write_text(README, encoding="utf-8")
    return root


def _model_writes(project: Path, request_id: str, relative: str, body: str) -> dict:
    """The local model path: a scripted model reply goes through the real file-block apply."""
    reply = f"Done.\n\n```{relative}\n{body}\n```\n"
    return run_general_request(project, f"request {request_id}", request_id=request_id, model_id=MODEL,
                               fake_complete=lambda _prompt: reply, start_preview_after=False)


def test_his_hand_edits_survive_a_stacked_keep_on_the_local_model_path(web_project):
    """The same on the path Nico's local models take: a model reply with file blocks, not a fake engine."""
    _model_writes(web_project, "req-1", "src/a.ts", "export const a = 'RED'")
    _hand_edits(web_project)
    second = _model_writes(web_project, "req-2", "src/b.ts", "export const b = 'BLUE'")
    assert load_meta(second["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/a.ts", "src/b.ts"]
    assert "README.md" not in second["diff"] and "notes.txt" not in second["diff"]
    kept = keep_changes("req-2", web_project)
    assert kept["applied"] == ["src/a.ts", "src/b.ts"] and kept["also_kept"] == ["req-1"]
    assert (web_project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert (web_project / "notes.txt").read_text(encoding="utf-8") == HIS_NOTES


def test_a_stack_of_three_keeps_his_edits(project):
    """V4: edits made while R1 waits, then R2, then R3. The third inherits from the second, which equals the first."""
    _fix(project)
    _hand_edits(project)
    _extra(project)
    _third(project)
    review = snapshot("req-3", repo=project)
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py", "src/third.py"]
    assert _baseline(project, "req-3") == _baseline(project, "req-1")
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py", "src/third.py"]
    assert kept["also_kept"] == ["req-2", "req-1"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert (project / "notes.txt").read_text(encoding="utf-8") == HIS_NOTES
    assert _history(project) == [("req-3", "kept"), ("req-2", "kept"), ("req-1", "kept")]


# --- A file he changed that the stack also changed: Keep says so and writes nothing ---------------------------


def test_keep_refuses_a_file_he_changed_that_the_stack_also_changed(project):
    """V2: he edits calc.py, which R1 changed. Keep R2 would overwrite his version, so it is refused."""
    _fix(project)
    (project / "src" / "calc.py").write_text(HIS_CALC, encoding="utf-8")
    _extra(project)
    before = _tree(project)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-2", project)
    assert refused.value.code == "conflict" and refused.value.detail == CONFLICT
    assert _tree(project) == before, "a refused Keep writes nothing"
    assert (project / "src" / "calc.py").read_text(encoding="utf-8") == HIS_CALC
    still = snapshot("req-2", repo=project)
    assert still["can_keep"] is True and still["can_discard"] is True and still["outcome"] == "review"
    # Discarding the newer request does not free the older one here: R1 changed the file he edited.
    assert discard_changes("req-2", repo=project)["discarded"] is True
    with pytest.raises(GeneralError) as older:
        keep_changes("req-1", project)
    assert older.value.code == "conflict"
    assert _tree(project) == before


def test_keep_refuses_a_file_he_created_where_the_stack_adds_one(project):
    """V3: he creates src/extra.py, the very path R2 adds. Keep R2 is refused; R1 can still be kept on its own."""
    _fix(project)
    (project / "src" / "extra.py").write_text("EXTRA = 'Nico'\n", encoding="utf-8")
    _extra(project)
    before = _tree(project)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-2", project)
    assert refused.value.code == "conflict" and refused.value.detail == CONFLICT
    assert _tree(project) == before
    still = snapshot("req-2", repo=project)
    assert still["can_keep"] is True and still["outcome"] == "review"
    discard_changes("req-2", repo=project)
    assert keep_changes("req-1", project)["applied"] == ["src/calc.py"], "the conflict came from R2 alone"
    assert (project / "src" / "extra.py").read_text(encoding="utf-8") == "EXTRA = 'Nico'\n"


def test_keep_refuses_when_he_deleted_a_file_the_stack_changed(project):
    _fix(project)
    (project / "src" / "calc.py").unlink()
    _extra(project)
    before = _tree(project)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-2", project)
    assert refused.value.code == "conflict" and refused.value.detail == CONFLICT
    assert _tree(project) == before and not (project / "src" / "calc.py").exists()


def test_keep_refuses_when_the_stack_deletes_a_file_he_edited(project):
    """The model's deletion must not take his edited file with it."""
    _run(project, "req-1", _deletes("src/util.py"))
    (project / "src" / "util.py").write_text("UTIL = 'Nico'\n", encoding="utf-8")
    _extra(project)
    before = _tree(project)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-2", project)
    assert refused.value.code == "conflict" and refused.value.detail == CONFLICT
    assert _tree(project) == before
    assert (project / "src" / "util.py").read_text(encoding="utf-8") == "UTIL = 'Nico'\n"


# --- Edits made while the newer request runs, and edits that are not about the stack --------------------------


def _during(project: Path, request_id: str, hook, engine) -> dict:
    """Run a request whose engine first calls `hook()`: the moment the request is still running."""

    def run(cwd: Path, text: str, cancel) -> None:
        hook()
        engine(cwd, text, cancel)

    return _run(project, request_id, run)


def test_an_unrelated_edit_made_while_the_newer_request_runs_survives(project):
    _fix(project)
    _during(project, "req-2", lambda: _hand_edits(project), _writes("src/extra.py", "EXTRA = 1\n"))
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert (project / "notes.txt").read_text(encoding="utf-8") == HIS_NOTES


def test_an_edit_to_a_stack_file_made_while_the_newer_request_runs_is_refused(project):
    _fix(project)
    _during(project, "req-2", lambda: (project / "src" / "calc.py").write_text(HIS_CALC, encoding="utf-8"),
            _writes("src/extra.py", "EXTRA = 1\n"))
    before = _tree(project)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-2", project)
    assert refused.value.code == "conflict"
    assert _tree(project) == before


def test_a_file_he_deleted_and_one_he_renamed_stay_that_way(project):
    """Unrelated to the stack: Keep must not bring a deleted file back or undo a rename."""
    _fix(project)
    (project / "README.md").unlink()
    (project / "src" / "util.py").rename(project / "src" / "util2.py")
    review = _extra(project)
    assert review["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    assert keep_changes("req-2", project)["applied"] == ["src/calc.py", "src/extra.py"]
    assert not (project / "README.md").exists()
    assert (project / "src" / "util2.py").is_file() and not (project / "src" / "util.py").exists()


# --- Deciding inside a stack -----------------------------------------------------------------------------------


def test_the_older_request_is_kept_alone_after_the_newer_one_is_discarded(project):
    """V5: per-step undo. Discard R2, then Keep R1 on its own; his edit stays and R2's file never lands."""
    _fix(project)
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    _extra(project)
    assert discard_changes("req-2", repo=project)["discarded"] is True
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["can_discard"] is True and older["blocked_by"] is None
    kept = keep_changes("req-1", project)
    assert kept["applied"] == ["src/calc.py"] and kept["also_kept"] == []
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert not (project / "src" / "extra.py").exists()
    assert _history(project) == [("req-2", "discarded"), ("req-1", "kept")]


def test_discarding_the_top_of_three_then_keeping_the_middle_one(project):
    _fix(project)
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    _extra(project)
    _third(project)
    discard_changes("req-3", repo=project)
    middle = snapshot("req-2", repo=project)
    assert middle["can_keep"] is True and middle["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["req-1"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert not (project / "src" / "third.py").exists()
    assert _history(project) == [("req-3", "discarded"), ("req-2", "kept"), ("req-1", "kept")]


def test_a_request_that_changed_nothing_between_two_others_keeps_his_edits_too(project):
    """Cut stack-survives: R2 changed nothing, so it has no result of its own. It lists no file and offers no Keep or
    Discard; R3 is built on R1 and one Keep takes both, so R2 is no part of the stack (it stays `review`)."""
    _fix(project)
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    second = _run(project, "req-2", lambda _cwd, _text, _cancel: None)
    assert second["facts"]["files"] == [] and second["can_keep"] is False and second["can_discard"] is False
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None, "R2 holds nothing back"
    third = _third(project)
    assert load_meta(third["project_id"], "req-3")["built_on"] == "req-1"
    kept = keep_changes("req-3", project)
    assert kept["applied"] == ["src/calc.py", "src/third.py"] and kept["also_kept"] == ["req-1"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert _history(project) == [("req-3", "kept"), ("req-2", "review"), ("req-1", "kept")]


def test_a_stack_without_hand_edits_behaves_as_it_did(project):
    """V6: nothing was edited, so nothing may look different: the link, the held-back older request, the Keep."""
    first = _fix(project)
    second = _extra(project)
    assert "built_on" not in load_meta(first["project_id"], "req-1")
    assert load_meta(second["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is False and older["can_discard"] is False and older["blocked_by"] == "req-2"
    assert older["summary"] == DECIDE_NEWER_FIRST
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["req-1"]
    assert _history(project) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_local_follow_up_on_a_waiting_hosted_result_keeps_his_edits(project):
    """The stack can start with a hosted result: a local request on top of it inherits that result's baseline."""
    _agent_fix(project)
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    second = _extra(project)
    assert load_meta(second["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/calc.py", "src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/calc.py", "src/extra.py"] and kept["also_kept"] == ["req-1"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README


# --- The baseline is inherited only from a waiting result, and only when it is all there ---------------------


def test_a_request_after_a_kept_one_starts_from_the_folder_as_it_is_now(project):
    """V7: R1 was kept, so nothing waits. R2 is not built on it and its baseline is the folder, edits included."""
    _fix(project)
    keep_changes("req-1", project)
    _hand_edits(project)
    second = _extra(project)
    record = _record(project, "req-2")
    assert record["built_on"] is None and "notes.txt" in record["baseline_files"]
    assert second["facts"]["files"] == ["src/extra.py"]
    assert keep_changes("req-2", project)["applied"] == ["src/extra.py"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README


def _lose_the_folder(older: Path) -> None:
    shutil.rmtree(older / "baseline")


def _empty_the_folder(older: Path) -> None:
    shutil.rmtree(older / "baseline")
    (older / "baseline").mkdir()


def _lose_a_file_from_the_folder(older: Path) -> None:
    (older / "baseline" / "README.md").unlink()


def _lose_the_index(older: Path) -> None:
    (older / "baseline.json").unlink()


def _corrupt_the_index(older: Path) -> None:
    (older / "baseline.json").write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("damage", [
    _lose_the_folder, _empty_the_folder, _lose_a_file_from_the_folder, _lose_the_index, _corrupt_the_index,
], ids=["folder-gone", "folder-emptied", "folder-partly-gone", "index-gone", "index-corrupt"])
def test_a_waiting_result_whose_baseline_is_not_all_there_is_not_built_on(project, damage):
    """F1: an empty baseline would list every file as added and refuse Keep for nothing, so the new request
    starts from the folder as it is now instead: no stack, no failure."""
    first = _fix(project)
    damage(request_dir(first["project_id"], "req-1"))
    _hand_edits(project)
    second = _extra(project)
    record = _record(project, "req-2")
    assert record["built_on"] is None
    assert "notes.txt" in record["baseline_files"], "its baseline is the folder, not an empty one"
    assert second["facts"]["files"] == ["src/extra.py"]
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/extra.py"] and kept["also_kept"] == []
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README


def test_a_continuation_keeps_a_baseline_of_its_own_the_folder_as_it_is_now(project):
    """continue_from is not part of this fix: it is built on the request it continues, but does not inherit."""
    _fix(project)
    _hand_edits(project)
    third = run_general_request(project, "carry on", request_id="req-3", fake_engine=_writes("src/third.py", "T = 3\n"),
                                continue_from="req-1", start_preview_after=False)
    record = _record(project, "req-3")
    assert record["built_on"] == "req-1" and "notes.txt" in record["baseline_files"]
    assert "src/third.py" in third["facts"]["files"] and "src/calc.py" in third["facts"]["files"]


def test_a_crash_while_the_workspace_is_made_leaves_no_half_record(project, monkeypatch):
    """meta.json is written last: a request that never got that far is not a request, and holds nothing back."""
    _fix(project)
    _hand_edits(project)

    def breaks(_root):
        raise RuntimeError("the disk fell over")

    with monkeypatch.context() as scoped:  # not monkeypatch.undo(): that would undo the HOME and data dir too
        scoped.setattr(coordinator, "ensure_ts_preview", breaks)
        with pytest.raises(RuntimeError):
            _extra(project)
    assert [row["request_id"] for row in list_requests(project)] == ["req-1"]
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["blocked_by"] is None
    assert keep_changes("req-1", project)["applied"] == ["src/calc.py"]
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
