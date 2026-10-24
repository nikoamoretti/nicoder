"""A request built on an older one that still waits (cut core-fixes, QA's stacked-requests defect).

On the local model path every request runs on the project's one working copy, so request 2 starts from
request 1's unreviewed result and keeping request 2 puts both into the project. Everything that reads where
a request stands has to agree about that: Keep closes the older request it carried, a question is not
told about it, History does not call it waiting, and the older request can not be kept or discarded alone
while the newer one still has changes to decide (its Keep would bring a discarded change back). Every engine
builds on the result that still waits, the hosted agent included. A Discard puts the working copy back to the
result the discarded request was built on, so it does not cut the stack. Requests that do not build on each other
(the older result cannot be inherited whole, here its baseline index is gone) stay separate.
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
UNDONE = "A later request undid these changes, so they were not added to your project."
REPLACED_CALC = (
    "These changes were replaced by a later request you kept. Nothing from them went into src/calc.py."
)
DECIDE_NEWER_FIRST = "A newer request in this project builds on this one, so Keep or Discard the newer one first."
STILL_RUNNING = (
    "A newer request in this project builds on this one and is still running, so let it finish or stop it first."
)

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
    """A hosted-agent run: like every other engine it builds on the result that still waits."""
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


def test_a_hosted_request_on_a_waiting_hosted_one_is_built_on_it_and_its_keep_closes_it(calc):
    """A hosted-agent run builds on the result that still waits, so keeping the newer one carries the older one in."""
    first = _agent_writes(calc, "req-1", "src/one.py", "ONE = 1\n")
    second = _agent_writes(calc, "req-2", "src/two.py", "TWO = 2\n")
    assert "built_on" not in load_meta(first["project_id"], "req-1")
    assert load_meta(second["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/one.py", "src/two.py"]
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False and older["blocked_by"] == "req-2" and older["summary"] == DECIDE_NEWER_FIRST
    assert waiting_note(calc, can_read=False).endswith("It adds src/one.py, src/two.py.")
    kept = keep_changes("req-2", calc)
    assert kept["also_kept"] == ["req-1"] and kept["applied"] == ["src/one.py", "src/two.py"]
    assert _history(calc) == [("req-2", "kept"), ("req-1", "kept")]
    assert waiting_note(calc, can_read=False) == ""


def _lose_the_baseline_index(project_id: str, request_id: str) -> None:
    """Make a waiting result one that cannot be built on: its baseline index is gone, so it can not be inherited
    whole. It can still be reviewed, kept and discarded from its baseline folder."""
    (request_dir(project_id, request_id) / "baseline.json").unlink()


def test_requests_that_start_from_the_folder_do_not_close_each_other(calc):
    """A request starts from the folder when the older result cannot be built on (here its baseline index is gone),
    so keeping it never carries the older one."""
    first = _agent_writes(calc, "req-1", "src/one.py", "ONE = 1\n")
    _lose_the_baseline_index(first["project_id"], "req-1")
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


def test_an_agent_run_after_a_local_one_builds_on_it(calc):
    _fix(calc)
    second = _agent_writes(calc, "req-2", "src/two.py", "TWO = 2\n")
    assert load_meta(second["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/calc.py", "src/two.py"], "the agent started from the local result"
    with pytest.raises(GeneralError) as refused:  # the newer request is decided first
        discard_changes("req-1", repo=calc)
    assert refused.value.code == "newer_waiting" and refused.value.detail == DECIDE_NEWER_FIRST
    assert keep_changes("req-2", calc)["also_kept"] == ["req-1"]


def test_discarding_the_newer_request_leaves_the_older_result_in_the_copy_for_the_next_request(calc):
    """After Discard the working copy is back at request 1's result, not at the folder: the next request is built on
    request 1, one Keep takes both, and the discarded request's file never lands."""
    _fix(calc)
    _extra(calc)
    discard_changes("req-2", repo=calc)
    at_start: dict = {}

    def third_engine(cwd: Path, text: str, cancel) -> None:
        at_start.update(calc=(cwd / "src" / "calc.py").read_text(encoding="utf-8"),
                        extra=(cwd / "src" / "extra.py").exists())
        _write("src/third.py", "THIRD = 3\n")(cwd, text, cancel)

    third = _run(calc, "req-3", third_engine)
    assert load_meta(third["project_id"], "req-3")["built_on"] == "req-1"
    assert at_start == {"calc": NEW, "extra": False}, "it starts in request 1's result without the discarded file"
    assert third["facts"]["files"] == ["src/calc.py", "src/third.py"]
    kept = keep_changes("req-3", calc)
    assert kept["also_kept"] == ["req-1"] and kept["applied"] == ["src/calc.py", "src/third.py"]
    older = snapshot("req-1", repo=calc)
    assert older["outcome"] == "kept" and older["summary"] == WENT_IN
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == NEW and not (calc / "src" / "extra.py").exists()


def test_an_older_request_the_later_keep_took_files_back_from_is_closed_and_cannot_be_kept_on_its_own(calc):
    """Request 2 undid request 1's change and added a file. Net-out: keeping it still closes request 1, because
    request 1's result is the base of request 2's, and records that none of its files went in (`replaced`). It used to
    stay keepable, and its Keep undid what he had just kept."""
    first = _fix(calc)

    def undo_and_add(cwd: Path, _text: str, _cancel) -> None:
        (cwd / "src" / "calc.py").write_text(OLD, encoding="utf-8")
        (cwd / "src" / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")

    second = _run(calc, "req-2", undo_and_add)
    assert second["facts"]["files"] == ["src/extra.py"], "request 1's change was undone on the way"
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    assert keep_changes("req-2", calc)["also_kept"] == ["req-1"]
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == OLD
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False and older["can_discard"] is False
    assert older["outcome"] == "replaced" and older["dropped"] == ["src/calc.py"] and older["kept_with"] == "req-2"
    assert older["summary"] == REPLACED_CALC
    assert _history(calc) == [("req-2", "kept"), ("req-1", "replaced")]
    before = _tree(calc)
    for decide in (lambda: keep_changes("req-1", calc), lambda: discard_changes("req-1", repo=calc)):
        with pytest.raises(GeneralError) as refused:
            decide()
        assert refused.value.code == "kept" and refused.value.detail == REPLACED_CALC
    assert _tree(calc) == before and (calc / "src" / "calc.py").read_text(encoding="utf-8") == OLD


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
    assert snapshot("req-1", repo=calc)["can_keep"] is False, "the review does not offer a Keep that can only fail"
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
    """Request 2 changed nothing (the copy stays request 1's result): it has no files, so its Keep can not bring
    anything back, and the older request is still keepable and discardable.

    Changed by the `undo-closes` cut: this was an undo (request 2 put the old text back). That now closes request 1
    (the next test); a request that changes nothing is what keeps this protection, so it is the one used here."""
    first = _fix(calc)
    second = _run(calc, "req-2", lambda _cwd, _text, _cancel: None)
    assert second["facts"]["files"] == [] and second["can_keep"] is False
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is True and older["can_discard"] is True, "nothing newer holds it back"
    assert discard_changes("req-1", repo=calc)["discarded"] is True


def test_a_newer_request_that_undid_the_older_one_closes_it(calc):
    """Request 2 undid request 1's change and ended normally: request 1 is closed as undone, with the true sentence.
    Discard of it still answers ok, with that sentence rather than "Already discarded."."""
    first = _fix(calc)
    second = _run(calc, "req-2", _write("src/calc.py", OLD))
    assert second["facts"]["files"] == [] and second["can_keep"] is False
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False and older["can_discard"] is False and older["outcome"] == "discarded"
    assert older["summary"] == UNDONE
    answer = discard_changes("req-1", repo=calc)
    assert answer["discarded"] is True and answer["detail"] == UNDONE
    assert _history(calc) == [("req-2", "review"), ("req-1", "discarded")]


def test_two_hosted_results_in_a_stack_are_decided_newest_first(calc):
    _agent_writes(calc, "req-1", "src/one.py", "ONE = 1\n")
    _agent_writes(calc, "req-2", "src/two.py", "TWO = 2\n")
    with pytest.raises(GeneralError) as refused:
        discard_changes("req-1", repo=calc)
    assert refused.value.code == "newer_waiting" and refused.value.detail == DECIDE_NEWER_FIRST
    assert discard_changes("req-2", repo=calc)["discarded"] is True
    assert snapshot("req-1", repo=calc)["can_keep"] is True
    assert keep_changes("req-1", calc)["applied"] == ["src/one.py"]


def test_requests_that_do_not_build_on_each_other_can_be_discarded_in_any_order(calc):
    """Independent: the second started from the folder because the first could not be built on (its baseline index
    is gone), so the older one can be discarded while the newer one waits."""
    first = _agent_writes(calc, "req-1", "src/one.py", "ONE = 1\n")
    _lose_the_baseline_index(first["project_id"], "req-1")
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


# --- The older request's snapshot says what the refusals say -------------------------------------------------


def test_the_snapshot_of_a_blocked_older_request_offers_neither_keep_nor_discard(calc):
    """QA round 2: both buttons failed with newer_waiting, yet the review offered them."""
    _fix(calc)
    _extra(calc)
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False and older["can_discard"] is False
    assert older["blocked_by"] == "req-2", "names the newer request"
    assert older["summary"] == DECIDE_NEWER_FIRST, "one plain sentence that says what to do"
    assert older["outcome"] == "review" and older["facts"]["outcome"] == "review", "it still waits"
    assert older["facts"]["files"] == ["src/calc.py"], "what it changed still shows"
    assert "return a + b" in older["diff"]
    newer = snapshot("req-2", repo=calc)
    assert newer["can_keep"] is True and newer["can_discard"] is True and newer["blocked_by"] is None
    assert newer["summary"] == "I changed 2 file(s). Review the preview, then Keep or Discard."


def test_the_blocked_snapshot_and_the_refusals_say_the_same_thing(calc):
    _fix(calc)
    _extra(calc)
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False and older["can_discard"] is False
    with pytest.raises(GeneralError) as keep:
        keep_changes("req-1", calc)
    with pytest.raises(GeneralError) as discard:
        discard_changes("req-1", repo=calc)
    assert keep.value.detail == discard.value.detail == older["summary"]
    assert keep.value.code == discard.value.code == "newer_waiting"


def test_a_blocked_request_shows_its_reason_instead_of_the_models_call_to_review(calc):
    first = _fix(calc)
    meta_file = request_dir(first["project_id"], "req-1") / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["model_commentary"] = "I fixed add. Review it, then Keep."
    meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    alone = snapshot("req-1", repo=calc)
    assert alone["can_keep"] is True and alone["summary"] == alone["model_commentary"] == meta["model_commentary"]
    _extra(calc)
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is False
    assert older["summary"] == DECIDE_NEWER_FIRST and older["model_commentary"] is None, (
        "the app shows the commentary first, so it must not repeat a call to review"
    )


def test_in_a_stack_of_three_the_request_to_decide_first_is_the_newest(calc):
    _fix(calc)
    _extra(calc)
    _run(calc, "req-3", _write("src/third.py", "THIRD = 3\n"))
    one, two, three = (snapshot(f"req-{n}", repo=calc) for n in (1, 2, 3))
    assert (one["can_keep"], two["can_keep"], three["can_keep"]) == (False, False, True)
    assert (one["can_discard"], two["can_discard"], three["can_discard"]) == (False, False, True)
    assert (one["blocked_by"], two["blocked_by"], three["blocked_by"]) == ("req-3", "req-3", None)
    discard_changes("req-3", repo=calc)
    two = snapshot("req-2", repo=calc)
    assert two["can_keep"] is True and two["can_discard"] is True and two["blocked_by"] is None
    assert two["summary"] == "I changed 2 file(s). Review the preview, then Keep or Discard."
    one = snapshot("req-1", repo=calc)
    assert one["can_keep"] is False and one["blocked_by"] == "req-2", "now the next one up is the one to decide"


def test_the_older_request_is_offered_again_once_the_newer_one_is_decided(calc):
    _fix(calc)
    _extra(calc)
    assert snapshot("req-1", repo=calc)["can_keep"] is False
    discard_changes("req-2", repo=calc)
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is True and older["can_discard"] is True and older["blocked_by"] is None
    assert older["summary"] == "I changed 1 file(s). Review the preview, then Keep or Discard."
    assert keep_changes("req-1", calc)["applied"] == ["src/calc.py"]


def test_the_blocked_snapshot_reaches_the_app_with_its_two_flags_false(calc):
    _fix(calc)
    _extra(calc)
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    reply = server.dispatch({"id": "1", "method": "snapshot", "params": {"episode_id": "req-1", "path": str(calc)}})
    assert reply["ok"] is True
    result = reply["result"]
    assert result["can_keep"] is False and result["can_discard"] is False and result["blocked_by"] == "req-2"
    assert result["facts"]["files"] == ["src/calc.py"] and result["diff"], "the Mac app still lists the files and diff"


# --- Keep waits for a newer request that is still running ----------------------------------------------------


def _during(project: Path, request_id: str, probe, engine=None) -> dict:
    """Run a request whose engine first calls `probe(candidate)`: the moment the request is still running."""
    write = engine or _write("src/extra.py", "EXTRA = 1\n")

    def run(cwd: Path, text: str, cancel) -> None:
        probe(cwd)
        write(cwd, text, cancel)

    return run_general_request(project, f"request {request_id}", request_id=request_id, fake_engine=run,
                               start_preview_after=False)


def _keep_outcome(project: Path, request_id: str) -> tuple[str, str]:
    """(code, detail) of a refused Keep, or ("kept", "") when it went through."""
    try:
        keep_changes(request_id, project)
    except GeneralError as exc:
        return exc.code, exc.detail
    return "kept", ""


def test_keeping_the_older_request_is_refused_while_the_newer_one_runs(calc):
    """QA round 2: Keep went through mid-run, and the newer request could then never be kept."""
    first = _fix(calc)
    before = _tree(calc)
    seen: list = []

    def probe(_cwd: Path) -> None:
        seen.append(_keep_outcome(calc, "req-1"))
        seen.append(_tree(calc) == before)
        seen.append("kept" in load_meta(first["project_id"], "req-1"))

    second = _during(calc, "req-2", probe)
    assert seen == [("busy", STILL_RUNNING), True, False], "refused in one plain sentence; nothing was written"
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    # Nothing was lost: once it has finished, the newer request is kept and carries the older one in with it.
    assert second["can_keep"] is True
    kept = keep_changes("req-2", calc)
    assert kept["also_kept"] == ["req-1"] and sorted(kept["applied"]) == ["src/calc.py", "src/extra.py"]
    assert (calc / "src" / "calc.py").read_text(encoding="utf-8") == NEW
    assert _history(calc) == [("req-2", "kept"), ("req-1", "kept")]


def test_the_older_requests_review_agrees_while_the_newer_one_runs(calc):
    _fix(calc)
    seen: list[dict] = []
    _during(calc, "req-2", lambda _cwd: seen.append(snapshot("req-1", repo=calc)))
    during = seen[0]
    assert during["can_keep"] is False and during["can_discard"] is False
    assert during["blocked_by"] == "req-2" and during["summary"] == STILL_RUNNING
    assert during["model_commentary"] is None and during["outcome"] == "review"
    assert during["facts"]["files"] == ["src/calc.py"] and "return a + b" in during["diff"]
    # When it has finished, the older request waits for the newer one to be decided instead.
    after = snapshot("req-1", repo=calc)
    assert after["can_keep"] is False and after["blocked_by"] == "req-2" and after["summary"] == DECIDE_NEWER_FIRST


def test_the_refusal_while_the_newer_request_runs_reaches_the_app_as_a_plain_sentence(calc):
    _fix(calc)
    server = RpcServer(runtime=AppRuntime(), out=io.StringIO())
    replies: list[dict] = []

    def rpc(method: str) -> dict:
        return server.dispatch({"id": method, "method": method, "params": {"episode_id": "req-1", "path": str(calc)}})

    _during(calc, "req-2", lambda _cwd: replies.extend([rpc("keep"), rpc("snapshot")]))
    kept, review = replies
    assert kept["ok"] is False and kept["error"] == {"code": "busy", "detail": STILL_RUNNING}
    assert review["ok"] is True and review["result"]["can_keep"] is False
    assert review["result"]["can_discard"] is False and review["result"]["summary"] == STILL_RUNNING


def test_a_request_given_no_id_holds_back_the_older_one_too(calc):
    """The app always names its request; a caller that does not still gets the same protection."""
    _fix(calc)
    seen: list = []

    def probe(_cwd: Path) -> None:
        seen.append(_keep_outcome(calc, "req-1"))

    def run(cwd: Path, text: str, cancel) -> None:
        probe(cwd)
        _write("src/extra.py", "EXTRA = 1\n")(cwd, text, cancel)

    review = run_general_request(calc, "no id given", fake_engine=run, start_preview_after=False)
    assert seen == [("busy", STILL_RUNNING)]
    assert review["request_id"] and review["request_id"] != "req-1"


def test_a_request_running_on_top_of_a_stack_holds_back_every_older_one(calc):
    _fix(calc)
    _extra(calc)
    seen: list = []

    def probe(_cwd: Path) -> None:
        seen.append(_keep_outcome(calc, "req-1"))
        seen.append(_keep_outcome(calc, "req-2"))
        seen.append([snapshot(f"req-{n}", repo=calc)["blocked_by"] for n in (1, 2)])

    _during(calc, "req-3", probe, engine=_write("src/third.py", "THIRD = 3\n"))
    assert seen == [("busy", STILL_RUNNING), ("busy", STILL_RUNNING), ["req-3", "req-3"]]
    assert not (calc / "src" / "extra.py").exists() and (calc / "src" / "calc.py").read_text(encoding="utf-8") == OLD
    assert keep_changes("req-3", calc)["also_kept"] == ["req-2", "req-1"]


def test_a_running_request_that_is_not_built_on_the_older_one_does_not_hold_back_its_keep(calc):
    """Only a request built on it matters. Here the hosted-agent run starts from the folder: req-1's baseline index
    is gone, so it can not be built on, and req-1 still waits."""
    first = _fix(calc)
    _lose_the_baseline_index(first["project_id"], "req-1")
    kept_midrun: list[dict] = []
    steps = iter([
        _reply(calls=[_call("write", {"path": "src/three.py", "content": "THREE = 3\n"}, "1")]),
        _reply("I wrote src/three.py."),
    ])

    def chat(_messages, _tools):
        if not kept_midrun:
            kept_midrun.append(keep_changes("req-1", calc))
        return next(steps)

    third = run_general_request(calc, "request req-3", request_id="req-3", agent_chat=chat,
                                start_preview_after=False)
    assert "built_on" not in load_meta(first["project_id"], "req-3")
    assert kept_midrun and kept_midrun[0]["applied"] == ["src/calc.py"]
    assert third["facts"]["files"] == ["src/three.py"]
    assert keep_changes("req-3", calc)["applied"] == ["src/three.py"]
    assert _history(calc) == [("req-3", "kept"), ("req-1", "kept")]


def test_a_running_request_built_on_the_older_one_after_a_discard_holds_back_its_keep(calc):
    """The mirror of the test above: after the Discard of req-2 the next hosted run IS built on req-1 (the copy is back
    at its result), so it holds req-1 back while it runs, and one Keep then takes both."""
    first = _fix(calc)
    _extra(calc)
    discard_changes("req-2", repo=calc)
    before = _tree(calc)
    seen: list = []
    steps = iter([
        _reply(calls=[_call("write", {"path": "src/three.py", "content": "THREE = 3\n"}, "1")]),
        _reply("I wrote src/three.py."),
    ])

    def chat(_messages, _tools):
        if not seen:
            seen.append(_keep_outcome(calc, "req-1"))
            seen.append(_tree(calc) == before)
        return next(steps)

    third = run_general_request(calc, "request req-3", request_id="req-3", agent_chat=chat,
                                start_preview_after=False)
    assert seen == [("busy", STILL_RUNNING), True], "refused in one plain sentence; nothing was written"
    assert load_meta(first["project_id"], "req-3")["built_on"] == "req-1"
    assert third["facts"]["files"] == ["src/calc.py", "src/three.py"]
    kept = keep_changes("req-3", calc)
    assert kept["also_kept"] == ["req-1"] and kept["applied"] == ["src/calc.py", "src/three.py"]
    assert _history(calc) == [("req-3", "kept"), ("req-2", "discarded"), ("req-1", "kept")]


def test_keeping_the_older_request_is_refused_while_a_hosted_follow_up_runs(calc):
    """A hosted-agent run is built on the result that waits, so it holds that result back like any other engine."""
    first = _fix(calc)
    before = _tree(calc)
    seen: list = []
    steps = iter([
        _reply(calls=[_call("write", {"path": "src/two.py", "content": "TWO = 2\n"}, "1")]),
        _reply("I wrote src/two.py."),
    ])

    def chat(_messages, _tools):
        if not seen:
            seen.append(_keep_outcome(calc, "req-1"))
            seen.append(_tree(calc) == before)
        return next(steps)

    second = run_general_request(calc, "request req-2", request_id="req-2", agent_chat=chat,
                                 start_preview_after=False)
    assert seen == [("busy", STILL_RUNNING), True], "refused in one plain sentence; nothing was written"
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1"
    assert second["facts"]["files"] == ["src/calc.py", "src/two.py"]
    kept = keep_changes("req-2", calc)
    assert kept["also_kept"] == ["req-1"] and kept["applied"] == ["src/calc.py", "src/two.py"]
    assert _history(calc) == [("req-2", "kept"), ("req-1", "kept")]


def test_a_failed_run_does_not_leave_the_older_request_locked(calc):
    first = _fix(calc)

    def breaks(_cwd: Path, _text: str, _cancel) -> None:
        raise RuntimeError("the engine fell over")

    with pytest.raises(RuntimeError):
        run_general_request(calc, "request req-2", request_id="req-2", fake_engine=breaks, start_preview_after=False)
    assert load_meta(first["project_id"], "req-2")["built_on"] == "req-1", "a record of the failed run is left"
    older = snapshot("req-1", repo=calc)
    assert older["can_keep"] is True and older["can_discard"] is True, "nothing is running or waiting on top of it"
    assert keep_changes("req-1", calc)["applied"] == ["src/calc.py"]


# --- The working copy after a decision -----------------------------------------------------------------------


def test_the_next_request_does_not_undo_an_older_request_kept_after_the_newer_one_was_discarded(calc):
    """Discard puts the working copy back to the older result; Keep then changes the folder, not the copy."""
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
