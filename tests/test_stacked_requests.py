"""A request built on an older one that still waits (cut core-fixes, QA's stacked-requests defect).

On the local model path every request runs on the project's one working copy, so request 2 starts from
request 1's unreviewed result and keeping request 2 puts both into the project. Everything that reads where
a request stands has to agree about that: Keep closes the older request it carried, a question is not
told about it, History does not call it waiting, and the older request can not be kept or discarded alone
while the newer one still has changes to decide (its Keep would bring a discarded change back). Requests
that do not build on each other (a hosted agent run starts from the folder) stay separate.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppRuntime, chat_request, discard_changes, keep_changes, snapshot
from local_coder.engines.opencode_config import data_root
from local_coder.general.coordinator import (
    GeneralError,
    list_requests,
    load_meta,
    request_dir,
    run_general_request,
)
from local_coder.general.waiting import waiting_note

OLD = "def add(a, b):\n    return a - b\n"
NEW = "def add(a, b):\n    return a + b\n"
FURTHER = "def add(a, b):\n    # numbers only\n    return a + b\n"
WENT_IN = "These changes went in with a later request you kept."
DECIDE_NEWER_FIRST = "A newer request in this project builds on this one, so Keep or Discard the newer one first."

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
def calc(home) -> Path:
    project = home / "projects" / "calc"
    (project / "src").mkdir(parents=True)
    (project / "src" / "calc.py").write_text(OLD, encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "calc"\n', encoding="utf-8")
    return project


def _write(relative: str, text: str):
    def engine(cwd: Path, _text: str, _cancel) -> None:
        (cwd / relative).parent.mkdir(parents=True, exist_ok=True)
        (cwd / relative).write_text(text, encoding="utf-8")

    return engine


def _run(project: Path, request_id: str, engine) -> dict:
    return run_general_request(project, f"request {request_id}", request_id=request_id, fake_engine=engine,
                               start_preview_after=False)


def _fix(project: Path, request_id: str = "req-1") -> dict:
    return _run(project, request_id, _write("src/calc.py", NEW))


def _extra(project: Path, request_id: str = "req-2") -> dict:
    return _run(project, request_id, _write("src/extra.py", "EXTRA = 1\n"))


def _agent_writes(project: Path, request_id: str, relative: str, text: str) -> dict:
    """A hosted-agent run: it starts from the project folder, whatever older results still wait."""
    steps = iter([
        _reply(calls=[_call("write", {"path": relative, "content": text}, "1")]),
        _reply(f"I wrote {relative}."),
    ])
    return run_general_request(project, f"request {request_id}", request_id=request_id,
                               agent_chat=lambda _messages, _tools: next(steps), start_preview_after=False)


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _history(project: Path) -> list[tuple[str, str]]:
    return [(row["request_id"], row["outcome"]) for row in list_requests(project)]


def _live(project_id: str) -> Path:
    return data_root() / "projects" / project_id / "live" / "candidate"


# --- Keeping the newer request closes the older one --------------------------------------------------------


def test_the_newer_request_is_built_on_the_older_one_that_still_waits(calc):
    first = _fix(calc)
    second = _extra(calc)
    assert "built_on" not in load_meta(first["project_id"], "req-1")
    assert load_meta(second["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/calc.py", "src/extra.py"], "request 2 carries request 1's change"


def test_keeping_the_newer_request_closes_the_older_one_it_carried(calc):
    """QA's repro: R1 waits, R2 on top, Keep R2, then ask. R1 is not waiting any more."""
    first = _fix(calc)
    _extra(calc)
    kept = keep_changes("req-2", calc)
    assert kept["ok"] is True and kept["also_kept"] == ["req-1"]
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == NEW
    assert (calc / "src" / "extra.py").is_file()
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False and older["can_discard"] is False
    assert older["outcome"] == "kept" and older["facts"]["outcome"] == "kept"
    assert older["summary"] == WENT_IN
    assert older["kept_with"] == "req-2"
    assert older["model_commentary"] is None
    assert older["facts"]["files"] == ["src/calc.py"], "what went in is still described"
    assert "return a + b" in older["diff"]
    assert load_meta(first["project_id"], "req-1")["kept_with"] == "req-2"
    assert snapshot("req-2", repo=calc)["summary"] == "These changes are in your project."
    assert snapshot("req-2", repo=calc)["kept_with"] is None
    assert _history(calc) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_question_after_that_keep_is_not_told_a_change_is_waiting(calc):
    _fix(calc)
    _extra(calc)
    keep_changes("req-2", calc)
    assert waiting_note(calc, can_read=True) == ""
    assert waiting_note(calc, can_read=False) == ""


def test_the_model_hears_nothing_about_a_change_that_is_already_in(calc, monkeypatch):
    from local_coder.agent import runner

    machine.write_machine_config(HOSTED)
    sent: list[list[dict]] = []

    def hosted_chat(cfg, body, *, thinking, **_):
        sent.append(json.loads(json.dumps(body["messages"])))
        return _reply("add(2, 3) is 5.")

    monkeypatch.setattr(runner, "hosted_chat", hosted_chat)
    _fix(calc)
    _extra(calc)
    chat_request(calc, "What does add(2, 3) return?")
    assert "not in the folder yet" in sent[-1][-1]["content"], "while the stack waits, the note is there"
    keep_changes("req-2", calc)
    chat_request(calc, "What does add(2, 3) return?")
    told = "\n".join(str(message.get("content") or "") for message in sent[-1])
    assert "not in the folder yet" not in told and "Local Coder's own record" not in told
    assert sent[-1][-1]["content"] == "What does add(2, 3) return?", "the question goes as it is"


def test_a_closed_older_request_cannot_be_kept_or_discarded_again(calc):
    _fix(calc)
    _extra(calc)
    keep_changes("req-2", calc)
    before = _tree(calc)
    with pytest.raises(GeneralError) as kept_again:
        keep_changes("req-1", calc)
    assert kept_again.value.code == "kept" and "already in your project" in kept_again.value.detail
    with pytest.raises(GeneralError) as discard:
        discard_changes("req-1", repo=calc)
    assert discard.value.code == "kept" and "cannot be discarded" in discard.value.detail
    assert "original project was not changed" not in discard.value.detail.lower()
    assert _tree(calc) == before
    assert _history(calc) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_later_request_that_changes_the_same_file_again_still_closes_the_older_one(calc):
    _fix(calc)
    _run(calc, "req-2", _write("src/calc.py", FURTHER))
    assert keep_changes("req-2", calc)["also_kept"] == ["req-1"]
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == FURTHER
    assert snapshot("req-1", repo=calc)["can_keep"] is False
    assert waiting_note(calc, can_read=True) == ""


def test_a_stack_of_three_is_closed_by_keeping_the_top(calc):
    _fix(calc)
    _extra(calc)
    _run(calc, "req-3", _write("src/third.py", "THIRD = 3\n"))
    assert keep_changes("req-3", calc)["also_kept"] == ["req-2", "req-1"]
    assert _history(calc) == [("req-3", "kept"), ("req-2", "kept"), ("req-1", "kept")]
    assert sorted(p.name for p in (calc / "src").iterdir()) == ["calc.py", "extra.py", "third.py"]


def test_a_new_request_after_the_keep_is_not_built_on_anything(calc):
    _fix(calc)
    _extra(calc)
    keep_changes("req-2", calc)
    third = _run(calc, "req-3", _write("src/third.py", "THIRD = 3\n"))
    assert "built_on" not in load_meta(third["project_id"], "req-3")
    assert third["facts"]["files"] == ["src/third.py"]


# --- Only what the Keep really carried is closed -------------------------------------------------------------


def test_requests_that_start_from_the_folder_do_not_close_each_other(calc):
    """A hosted-agent run begins at the project folder, so keeping one never carries an older one."""
    _agent_writes(calc, "req-1", "src/one.py", "ONE = 1\n")
    second = _agent_writes(calc, "req-2", "src/two.py", "TWO = 2\n")
    assert "built_on" not in load_meta(second["project_id"], "req-2")
    assert second["facts"]["files"] == ["src/two.py"]
    kept = keep_changes("req-2", calc)
    assert kept["also_kept"] == []
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is True and older["can_discard"] is True and older["outcome"] == "review"
    assert _history(calc) == [("req-2", "kept"), ("req-1", "review")]
    assert waiting_note(calc, can_read=False).endswith("It adds src/one.py.")
    assert keep_changes("req-1", calc)["applied"] == ["src/one.py"]


def test_an_agent_run_after_a_local_one_does_not_build_on_it(calc):
    _fix(calc)
    second = _agent_writes(calc, "req-2", "src/two.py", "TWO = 2\n")
    assert "built_on" not in load_meta(second["project_id"], "req-2")
    assert second["facts"]["files"] == ["src/two.py"], "the agent started from the folder"
    discard_changes("req-1", repo=calc)  # nothing is built on it, so it can be discarded
    assert keep_changes("req-2", calc)["also_kept"] == []


def test_discarding_the_newer_request_cuts_the_stack(calc):
    """After Discard the working copy is back at the folder, so the next request is not built on request 1."""
    _fix(calc)
    _extra(calc)
    discard_changes("req-2", repo=calc)
    third = _run(calc, "req-3", _write("src/third.py", "THIRD = 3\n"))
    assert "built_on" not in load_meta(third["project_id"], "req-3")
    assert keep_changes("req-3", calc)["also_kept"] == []
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is True and older["outcome"] == "review", "request 1 still waits: its change is not in"
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == OLD


def test_an_older_request_whose_files_the_keep_did_not_write_stays_open(calc):
    """Request 2 undid request 1's change and added a file: keeping it does not carry request 1 in."""
    first = _fix(calc)

    def undo_and_add(cwd: Path, _text: str, _cancel) -> None:
        (cwd / "src" / "calc.py").write_text(OLD, encoding="utf-8")
        (cwd / "src" / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")

    second = _run(calc, "req-2", undo_and_add)
    assert second["facts"]["files"] == ["src/extra.py"], "request 1's change was undone on the way"
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    assert keep_changes("req-2", calc)["also_kept"] == []
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == OLD
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is True and older["outcome"] == "review", "its change never went in"
    assert _history(calc) == [("req-2", "kept"), ("req-1", "review")]
    assert discard_changes("req-1", repo=calc)["detail"] == "Discarded. The original project was not changed."


def test_a_keep_that_is_refused_closes_nothing(calc):
    first = _fix(calc)
    _extra(calc)
    (calc / "src" / "calc.py").write_text("def add(a, b):\n    return 0  # edited by hand\n", encoding="utf-8")
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-2", calc)
    assert refused.value.code == "conflict"
    assert "kept" not in load_meta(first["project_id"], "req-1")
    assert "kept" not in load_meta(first["project_id"], "req-2")
    assert _history(calc) == [("req-2", "review"), ("req-1", "review")]
    assert not (calc / "src" / "extra.py").exists()


def test_a_damaged_older_record_does_not_fail_the_keep(calc):
    first = _fix(calc)
    _extra(calc)
    (request_dir(first["project_id"], "req-1") / "meta.json").write_text("{not json", encoding="utf-8")
    kept = keep_changes("req-2", calc)
    assert kept["ok"] is True and kept["also_kept"] == []
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == NEW


# --- The mirror case: deciding the older request alone -----------------------------------------------------


def test_discarding_the_older_request_is_refused_while_the_newer_one_waits(calc):
    first = _fix(calc)
    _extra(calc)
    before = _tree(calc)
    live_before = _tree(_live(first["project_id"]))
    with pytest.raises(GeneralError) as refused:
        discard_changes("req-1", repo=calc)
    assert refused.value.code == "newer_waiting"
    assert refused.value.detail == DECIDE_NEWER_FIRST, "one plain sentence"
    assert _tree(calc) == before
    assert _tree(_live(first["project_id"])) == live_before, "a refused Discard must not touch the working copy"
    assert "discarded" not in load_meta(first["project_id"], "req-1")
    assert snapshot("req-1", repo=calc)["outcome"] == "review"
    # Nothing came back and nothing false was said: keeping request 2 now carries request 1 in, as shown.
    assert keep_changes("req-2", calc)["also_kept"] == ["req-1"]
    assert _history(calc) == [("req-2", "kept"), ("req-1", "kept")]


def test_keeping_the_older_request_is_refused_while_the_newer_one_waits(calc):
    """Keeping it alone would leave request 2 unable to be kept (its baseline lacks request 1's change)."""
    _fix(calc)
    _extra(calc)
    before = _tree(calc)
    with pytest.raises(GeneralError) as refused:
        keep_changes("req-1", calc)
    assert refused.value.code == "newer_waiting" and refused.value.detail == DECIDE_NEWER_FIRST
    assert _tree(calc) == before
    assert snapshot("req-1", repo=calc)["can_keep"] is True
    assert keep_changes("req-2", calc)["ok"] is True


def test_deciding_the_newer_request_first_frees_the_older_one(calc):
    _fix(calc)
    _extra(calc)
    before = _tree(calc)
    discard_changes("req-2", repo=calc)
    assert discard_changes("req-1", repo=calc)["discarded"] is True
    assert _tree(calc) == before
    assert _history(calc) == [("req-2", "discarded"), ("req-1", "discarded")]


def test_the_older_request_can_be_kept_once_the_newer_one_is_discarded(calc):
    _fix(calc)
    _extra(calc)
    discard_changes("req-2", repo=calc)
    assert keep_changes("req-1", calc)["applied"] == ["src/calc.py"]
    assert not (calc / "src" / "extra.py").exists()
    assert _history(calc) == [("req-2", "discarded"), ("req-1", "kept")]


def test_a_newer_request_with_nothing_to_decide_does_not_block_the_older_one(calc):
    """Request 2 undid request 1's change: it has no files, so its Keep can not bring anything back."""
    first = _fix(calc)
    second = _run(calc, "req-2", _write("src/calc.py", OLD))
    assert second["facts"]["files"] == [] and second["can_keep"] is False
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    assert discard_changes("req-1", repo=calc)["discarded"] is True


def test_requests_that_do_not_build_on_each_other_can_be_discarded_in_any_order(calc):
    _agent_writes(calc, "req-1", "src/one.py", "ONE = 1\n")
    _agent_writes(calc, "req-2", "src/two.py", "TWO = 2\n")
    assert discard_changes("req-1", repo=calc)["discarded"] is True
    assert snapshot("req-2", repo=calc)["can_keep"] is True
    assert keep_changes("req-2", calc)["applied"] == ["src/two.py"]


def test_the_refusal_reaches_the_app_as_a_plain_sentence(calc):
    _fix(calc)
    _extra(calc)
    out = io.StringIO()
    server = RpcServer(runtime=AppRuntime(), out=out)
    reply = server.dispatch({"id": "1", "method": "discard", "params": {"episode_id": "req-1", "path": str(calc)}})
    assert reply["ok"] is False
    assert reply["error"]["detail"] == DECIDE_NEWER_FIRST and reply["error"]["code"] == "newer_waiting"
    kept = server.dispatch({"id": "2", "method": "keep", "params": {"episode_id": "req-2", "path": str(calc)}})
    assert kept["ok"] is True and kept["result"]["also_kept"] == ["req-1"]


# --- The working copy after a decision -----------------------------------------------------------------------


def test_the_next_request_does_not_undo_an_older_request_kept_after_the_newer_one_was_discarded(calc):
    """Discard puts the working copy back to the folder; Keep then changes the folder, not the copy."""
    _fix(calc)
    _extra(calc)
    discard_changes("req-2", repo=calc)
    assert keep_changes("req-1", calc)["applied"] == ["src/calc.py"]
    third = _run(calc, "req-3", _write("src/third.py", "THIRD = 3\n"))
    assert third["facts"]["files"] == ["src/third.py"], "request 1's change is in the folder, not part of request 3"
    assert "built_on" not in load_meta(third["project_id"], "req-3")


def test_a_request_not_built_on_a_waiting_result_starts_from_the_folder_as_it_is_now(calc):
    first = _fix(calc)
    discard_changes("req-1", repo=calc)
    (calc / "README.md").write_text("edited by hand\n", encoding="utf-8")
    second = _extra(calc, "req-2")
    assert second["facts"]["files"] == ["src/extra.py"], "the hand edit is not undone by the sticky copy"
    assert (_live(first["project_id"]) / "README.md").read_text(encoding="utf-8") == "edited by hand\n"
    assert keep_changes("req-2", calc)["applied"] == ["src/extra.py"]
    assert (calc / "README.md").read_text(encoding="utf-8") == "edited by hand\n"


# --- Where the link comes from -----------------------------------------------------------------------------


def test_a_continuation_is_built_on_the_request_it_continues(calc):
    _fix(calc)
    _extra(calc)
    third = run_general_request(
        calc, "carry on", request_id="req-3", fake_engine=_write("src/third.py", "THIRD = 3\n"),
        continue_from="req-1", start_preview_after=False,
    )
    assert load_meta(third["project_id"], "req-3")["built_on"] == "req-1"
    assert third["facts"]["files"] == ["src/calc.py", "src/third.py"], "request 2's extra file is not part of it"


def test_old_records_without_a_link_are_left_alone(calc):
    """Results saved before requests were linked behave as they did: nothing is closed or refused."""
    first = _fix(calc)
    _extra(calc)
    path = request_dir(first["project_id"], "req-2") / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.pop("built_on")
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    assert keep_changes("req-2", calc)["also_kept"] == []
    assert snapshot("req-1", repo=calc)["outcome"] == "review"
    assert discard_changes("req-1", repo=calc)["discarded"] is True
