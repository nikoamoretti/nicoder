"""stacked-resync: Keep never reverts what Nico changed in the folder himself.

A request built on a result that still waits (the local path: it runs on the project's sticky working copy)
starts from that result, not from the folder. Its baseline has to be that result's baseline, or the diff and
Keep would measure from two different starting points, and whatever Nico changed in the folder meanwhile
would show up as the model's work and be reverted by Keep. The hosted agent is the other way round: it starts
from the folder (his edits included), so it must never carry or inherit anything from a waiting result.

Every scenario here edits the folder by hand while a result waits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_service import keep_changes, snapshot
from local_coder.general import coordinator
from local_coder.general.coordinator import GeneralError, list_requests, load_meta, request_dir, run_general_request

OLD = "def add(a, b):\n    return a - b\n"
NEW = "def add(a, b):\n    return a + b\n"
README = "# calc\n"
HIS_README = "# calc\nNico's own paragraph\n"
HIS_NOTES = "Nico's notes\n"
CONFLICT = "Some files changed in the project while this request was running."

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


def _run(project: Path, request_id: str, engine) -> dict:
    return run_general_request(project, f"request {request_id}", request_id=request_id, fake_engine=engine,
                               start_preview_after=False)


def _fix(project: Path, request_id: str = "req-1") -> dict:
    return _run(project, request_id, _writes("src/calc.py", NEW))


def _extra(project: Path, request_id: str = "req-2") -> dict:
    return _run(project, request_id, _writes("src/extra.py", "EXTRA = 1\n"))


def _hand_edits(project: Path) -> None:
    """What Nico does in the folder while a result waits: edits a file and adds one."""
    (project / "README.md").write_text(HIS_README, encoding="utf-8")
    (project / "notes.txt").write_text(HIS_NOTES, encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


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
    """A hosted-agent run that fixes add(): it starts from the folder."""
    script = _Script(
        _reply(calls=[_call("write", {"path": "src/calc.py", "content": NEW}, "w")]), _reply("Fixed add()."),
    )
    return run_general_request(project, f"request {request_id}", request_id=request_id, agent_chat=script,
                               start_preview_after=False)


# --- The hosted agent starts from the folder, with his edits, and never carries a waiting result ---------------


def test_a_hosted_follow_up_starts_from_the_folder_with_his_edits(project):
    """H1: the follow-up works on another file. It sees his notes.txt, not the waiting fix, and its Keep is clean."""
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
    assert "notes.txt" in seen, "the agent works on the folder as it is now"
    assert "return a - b" in seen and "return a + b" not in seen, "it does not see the fix that still waits"
    assert at_first_call["built_on"] is None, "not built on the waiting result"
    assert "notes.txt" in at_first_call["baseline_files"], "its baseline is the folder as it is now"
    assert second["facts"]["files"] == ["src/extra.py"]
    older = snapshot("req-1", repo=project)
    assert older["can_keep"] is True and older["outcome"] == "review", "nothing holds the older request back"
    kept = keep_changes("req-2", project)
    assert kept["applied"] == ["src/extra.py"] and kept["also_kept"] == []
    assert (project / "README.md").read_text(encoding="utf-8") == HIS_README
    assert (project / "notes.txt").read_text(encoding="utf-8") == HIS_NOTES
    assert keep_changes("req-1", project)["applied"] == ["src/calc.py"]
    assert _history(project) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_hosted_follow_up_on_the_same_file_does_not_see_the_waiting_fix(project):
    """H2 documents today's hosted behaviour, left alone by this cut: the older Keep is still offered, and fails."""
    _agent_fix(project)
    script = _Script(
        _reply(calls=[_call("read", {"path": "src/calc.py"}, "a")]),
        _reply(calls=[_call("write", {"path": "src/calc.py", "content": OLD + "# documented\n"}, "b")]),
        _reply("Documented add()."),
    )
    second = run_general_request(project, "request req-2", request_id="req-2", agent_chat=script,
                                 start_preview_after=False)
    assert "return a - b" in script.tool_text(), "it reads the folder, which still has the old code"
    assert second["facts"]["files"] == ["src/calc.py"]
    assert keep_changes("req-2", project)["applied"] == ["src/calc.py"]
    assert snapshot("req-1", repo=project)["can_keep"] is True
    before = _tree(project)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-1", project)
    assert refused.value.code == "conflict" and refused.value.detail == CONFLICT
    assert _tree(project) == before, "a refused Keep writes nothing"
    assert _history(project) == [("req-2", "kept"), ("req-1", "review")]


def test_a_hosted_run_is_never_built_on_a_waiting_result_not_even_for_a_moment(project, monkeypatch):
    """The link must not be recorded and taken back: for that moment the older request would be held back."""
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
    script.hook = lambda: during.update(snapshot("req-1", repo=project))
    run_general_request(project, "request req-2", request_id="req-2", agent_chat=script, start_preview_after=False)
    assert made["in_memory"] is None and made["on_disk"] is None
    assert "notes.txt" in made["baseline_files"]
    assert during["can_keep"] is True and during["blocked_by"] is None, "the older request is not held back"


def test_the_engine_is_decided_once_for_the_workspace_and_the_run(project, monkeypatch):
    """With a hosted model selected the request reads the engine choice one time and never carries."""
    from local_coder.agent import runner

    machine.write_machine_config(HOSTED)
    _fix(project)
    _hand_edits(project)
    asked: list[bool] = []
    real = coordinator._agent_capable
    monkeypatch.setattr(coordinator, "_agent_capable", lambda: asked.append(True) or real())
    script = _Script(_reply(calls=[_call("write", {"path": "src/extra.py", "content": "EXTRA = 1\n"}, "a")]),
                     _reply("Added extra.py."))
    at_first_call: dict = {}
    script.hook = lambda: at_first_call.update(_record(project, "req-2"))
    monkeypatch.setattr(runner, "hosted_tool_chat", lambda _cfg, cancel=None: script)
    review = run_general_request(project, "request req-2", request_id="req-2", start_preview_after=False)
    assert asked == [True], "one decision, not one for the workspace and another for the run"
    assert review["engine"] == "agent"
    assert at_first_call["built_on"] is None and "notes.txt" in at_first_call["baseline_files"]
    assert review["facts"]["files"] == ["src/extra.py"]
    assert keep_changes("req-2", project)["applied"] == ["src/extra.py"]
