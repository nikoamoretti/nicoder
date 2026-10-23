"""A question asked while a result waits for Keep describes that result (cut core-fixes, problem 2).

The engine tells the model, from the request's own record and never from model text, that the result is
not in the folder yet, which files it changes and how to read it (read-only). Both question paths get it:
the hosted agent and the local llama.cpp chat. The note is not saved in the chat, a question changes
nothing, and the waiting copy is never opened as a project.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from local_coder import machine
from local_coder.agent.access import AccessPolicy
from local_coder.agent.tools import ToolBox
from local_coder.app_service import chat_request, discard_changes, keep_changes, load_prefs
from local_coder.engines.opencode_config import data_root
from local_coder.general.chat_log import load_chat
from local_coder.general.coordinator import project_id_for, request_dir, run_general_request

QUESTION = "Did the fix work? What does add(2, 3) return now with the code in src/calc.py?"
OLD = "def add(a, b):\n    return a - b\n"
NEW = "def add(a, b):\n    return a + b\n"
NOT_IN_FOLDER = "not in the folder yet"

HOSTED = machine.MachineConfig(
    model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b",
    endpoint="https://integrate.api.nvidia.com",
    provider="nvidia",
    api_key_name="NVIDIA_API_KEY",
    n_ctx=131072,
    max_tokens=16384,
    quantization="hosted",
)


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Recorder:
    """A scripted hosted model that remembers everything it was sent."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.sent: list[list[dict]] = []
        self.tools: list[list[str]] = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        self.sent.append(json.loads(json.dumps(messages)))
        self.tools.append([tool["function"]["name"] for tool in tools])
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply

    def seen(self) -> str:
        """Everything the model received in its first call."""
        return "\n".join(str(message.get("content") or "") for message in self.sent[0])

    def note(self) -> str:
        """What came with the question in its first call: the question's own message minus the question."""
        return str(self.sent[0][-1]["content"]).removeprefix(QUESTION).strip()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake HOME with the data dir inside it, so the question agent may read the waiting copy."""
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


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _fix(project: Path, request_id: str = "req-fix", text: str = "Fix add so it adds instead of subtracting.") -> dict:
    def engine(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "calc.py").write_text(NEW, encoding="utf-8")

    return run_general_request(project, text, request_id=request_id, fake_engine=engine, start_preview_after=False)


def _use_hosted(monkeypatch, recorder: Recorder) -> None:
    from local_coder.agent import runner

    machine.write_machine_config(HOSTED)
    monkeypatch.setattr(
        runner, "hosted_chat", lambda cfg, body, *, thinking, **_: recorder(body["messages"], body.get("tools") or []),
    )


# --- The hosted agent -------------------------------------------------------------------------------------


def test_the_agent_is_told_what_waits_and_how_to_read_it(calc, monkeypatch):
    review = _fix(calc)
    recorder = Recorder(_reply("The waiting copy returns 5; the change is not kept yet."))
    _use_hosted(monkeypatch, recorder)
    result = chat_request(calc, QUESTION)
    seen = recorder.seen()
    assert "src/calc.py" in seen
    assert NOT_IN_FOLDER in seen
    assert "Keep or Discard" in seen
    assert "Fix add so it adds instead of subtracting." in seen, "the request's title"
    assert "made just now" in seen, "its age"
    candidate = request_dir(review["project_id"], "req-fix") / "candidate"
    assert str(candidate) in seen, "a way to read it"
    assert "read-only" in seen
    # Told as a note after the question, not as the question itself.
    assert recorder.sent[0][-1]["content"].startswith(QUESTION)
    assert result["open_project"] is None


def test_the_note_is_not_saved_in_the_chat(calc, monkeypatch):
    _fix(calc)
    _use_hosted(monkeypatch, Recorder(_reply("It is waiting for you to Keep.")))
    result = chat_request(calc, QUESTION)
    turns = load_chat(calc)["turns"]
    assert [(turn["role"], turn["text"]) for turn in turns[-2:]] == [
        ("user", QUESTION), ("assistant", "It is waiting for you to Keep."),
    ]
    assert NOT_IN_FOLDER not in json.dumps(turns)
    assert [turn["text"] for turn in result["turns"][-2:]] == [QUESTION, "It is waiting for you to Keep."]


def test_the_saved_question_transcript_shows_the_model_was_told(calc, monkeypatch):
    _fix(calc)
    _use_hosted(monkeypatch, Recorder(_reply("Waiting.")))
    chat_request(calc, QUESTION)
    transcripts = sorted((data_root() / "agent-runs").glob("*/transcript.json"))
    assert len(transcripts) == 1
    saved = json.loads(transcripts[0].read_text(encoding="utf-8"))
    told = "\n".join(str(message.get("content") or "") for message in saved["messages"])
    assert "src/calc.py" in told and NOT_IN_FOLDER in told


def test_the_agent_can_read_the_waiting_copy_with_the_path_it_was_given(calc, monkeypatch):
    """The question agent's read rules already allow the data dir inside the home folder."""
    _fix(calc)

    def read_the_waiting_copy(messages):
        note = str(messages[-1]["content"])
        folder = re.search(r"in (/\S+?/candidate),", note)
        assert folder, note
        return _reply(calls=[_call("read", {"path": f"{folder.group(1)}/src/calc.py"}, "1")])

    def answer_from_it(messages):
        output = str(messages[-1]["content"])
        assert "return a + b" in output, output
        return _reply("add(2, 3) is 5 in the waiting copy, which is not kept yet.")

    recorder = Recorder(read_the_waiting_copy, answer_from_it)
    _use_hosted(monkeypatch, recorder)
    result = chat_request(calc, QUESTION)
    assert result["summary"].startswith("add(2, 3) is 5")
    assert result["status"] == "answered"


def test_a_question_changes_nothing_and_offers_no_way_to_write(calc, monkeypatch):
    review = _fix(calc)
    candidate = request_dir(review["project_id"], "req-fix") / "candidate"
    project_before, waiting_before = _tree(calc), _tree(candidate)
    recorder = Recorder(_reply("Waiting."))
    _use_hosted(monkeypatch, recorder)
    chat_request(calc, QUESTION)
    assert _tree(calc) == project_before
    assert _tree(candidate) == waiting_before
    assert not {"write", "edit"} & set(recorder.tools[0])
    assert "bash" in recorder.tools[0], "bash stays, inside its sandbox"


def test_the_waiting_copy_is_never_opened_as_a_project(calc, monkeypatch):
    review = _fix(calc)
    candidate = request_dir(review["project_id"], "req-fix") / "candidate"
    recorder = Recorder(
        _reply(calls=[_call("open_project", {"path": str(candidate)}, "1")]),
        lambda messages: _reply(f"It could not be opened: {messages[-1]['content']}"),
    )
    _use_hosted(monkeypatch, recorder)
    result = chat_request(calc, QUESTION)
    assert result["open_project"] is None
    assert "not a project" in result["summary"]
    assert str(candidate) not in [row["path"] for row in load_prefs()["recent_projects"]]


def test_a_read_only_look_at_the_waiting_copy_does_not_switch_projects(home, calc, monkeypatch):
    """With no project open the app opens the repo most read files belong to; never the waiting copy."""
    review = _fix(calc)
    candidate = request_dir(review["project_id"], "req-fix") / "candidate"
    for name in ("one.py", "two.py", "three.py"):
        (candidate / "src" / name).write_text(f"x = '{name}'\n", encoding="utf-8")
    reads = [_call("read", {"path": str(candidate / "src" / name)}, name) for name in ("one.py", "two.py", "three.py")]
    _use_hosted(monkeypatch, Recorder(_reply(calls=reads), _reply("Three small modules.")))
    result = chat_request(None, "What is in the waiting copy?")
    assert result["open_project"] is None
    assert "turns" not in result


def test_open_project_refuses_a_path_under_the_data_root(home, calc):
    review = _fix(calc)
    candidate = request_dir(review["project_id"], "req-fix") / "candidate"
    box = ToolBox(
        AccessPolicy(home=home, project=calc), cwd=calc, run_tmp=home / "tmp", allow_open_project=True,
    )
    for path in (candidate, data_root(), data_root() / "projects" / review["project_id"]):
        outcome = box.call("open_project", {"path": str(path)})
        assert outcome.ok is False, path
        assert "not a project" in outcome.text
    assert box.opened is None
    # A real project next to it still opens.
    assert box.call("open_project", {"path": str(calc)}).ok is True and box.opened == calc


# --- The note follows the state ---------------------------------------------------------------------------


def _ask(project: Path, monkeypatch) -> Recorder:
    recorder = Recorder(_reply("Okay."))
    _use_hosted(monkeypatch, recorder)
    chat_request(project, QUESTION)
    return recorder


def test_the_note_follows_the_state_of_the_project(calc, monkeypatch):
    # (a) no request yet: the question goes as it is
    assert _ask(calc, monkeypatch).note() == ""
    # (b) a result is waiting
    _fix(calc, "req-1")
    waiting = _ask(calc, monkeypatch).note()
    assert NOT_IN_FOLDER in waiting and "src/calc.py" in waiting
    # (c) after Keep
    keep_changes("req-1", calc)
    assert _ask(calc, monkeypatch).note() == ""

    # (d) a second result, then Discard
    def second(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")

    run_general_request(calc, "Add extra", request_id="req-2", fake_engine=second, start_preview_after=False)
    waiting = _ask(calc, monkeypatch).note()
    assert NOT_IN_FOLDER in waiting and "src/extra.py" in waiting and "src/calc.py" not in waiting
    discard_changes("req-2", repo=calc)
    assert _ask(calc, monkeypatch).note() == ""


def test_the_newest_open_request_that_changed_files_is_the_waiting_one(calc):
    from local_coder.general.waiting import find_waiting_result, waiting_note

    _fix(calc, "req-old")
    time.sleep(0.01)
    # A later hosted request that only answered is built on the waiting result but has no result of its own: it lists
    # no file, so the waiting result is still the request that made it, and the note names that request.
    answered = run_general_request(
        calc, "Fix anything that is wrong", request_id="req-empty",
        agent_chat=Recorder(_reply("Nothing needed changing.")), start_preview_after=False,
    )
    assert answered["facts"]["files"] == []
    waiting = find_waiting_result(calc)
    assert waiting is not None and waiting.request_id == "req-old" and waiting.files == ["src/calc.py"]
    assert "Fix add so it adds instead of subtracting." in waiting_note(calc, can_read=True), "its own title"
    time.sleep(0.01)

    def other(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "other.py").write_text("y = 2\n", encoding="utf-8")

    run_general_request(calc, "Add other", request_id="req-new", fake_engine=other, start_preview_after=False)
    newest = find_waiting_result(calc)
    assert newest is not None and newest.request_id == "req-new"


def test_a_request_that_only_answered_does_not_hide_a_result_it_is_not_built_on(calc):
    """The note names a result that really has files: an open request with none is passed over. Here the answering
    request is built on nothing, because a Discard of the request built on req-old put the working copy back."""
    from local_coder.general.waiting import find_waiting_result

    _fix(calc, "req-old")
    time.sleep(0.01)

    def other(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "other.py").write_text("y = 2\n", encoding="utf-8")

    run_general_request(calc, "Add other", request_id="req-mid", fake_engine=other, start_preview_after=False)
    discard_changes("req-mid", repo=calc)
    time.sleep(0.01)
    answered = run_general_request(
        calc, "Fix anything that is wrong", request_id="req-empty",
        agent_chat=Recorder(_reply("Nothing needed changing.")), start_preview_after=False,
    )
    assert answered["facts"]["files"] == []
    waiting = find_waiting_result(calc)
    assert waiting is not None and waiting.request_id == "req-old" and waiting.files == ["src/calc.py"]


def test_nothing_is_waiting_while_a_request_is_still_running(calc):
    from local_coder.general.coordinator import _request_running
    from local_coder.general.waiting import find_waiting_result

    _fix(calc)
    with _request_running(project_id_for(calc)):
        assert find_waiting_result(calc) is None
    assert find_waiting_result(calc) is not None


def test_a_bad_record_never_fails_a_question(calc, monkeypatch):
    from local_coder.general.waiting import waiting_note

    review = _fix(calc)
    (request_dir(review["project_id"], "req-fix") / "meta.json").write_text("{not json", encoding="utf-8")
    recorder = Recorder(_reply("Okay."))
    _use_hosted(monkeypatch, recorder)
    result = chat_request(calc, QUESTION)
    assert result["summary"] == "Okay."
    assert NOT_IN_FOLDER not in recorder.seen()
    # A record that points at nothing is skipped too.
    assert waiting_note(calc / "missing", can_read=True) == ""
    assert waiting_note(None, can_read=True) == ""


def test_asking_about_a_project_with_no_record_creates_nothing(calc, monkeypatch):
    _use_hosted(monkeypatch, Recorder(_reply("Okay.")))
    chat_request(calc, QUESTION)
    assert not (data_root() / "projects").exists()


def test_age_reads_in_plain_words():
    from local_coder.general.waiting import age_text

    assert age_text(5) == "just now"
    assert age_text(60) == "1 minute ago"
    assert age_text(5 * 60) == "5 minutes ago"
    assert age_text(2 * 3600) == "2 hours ago"
    assert age_text(3 * 86400) == "3 days ago"


# --- The local llama.cpp chat ----------------------------------------------------------------------------


def _install_fake_llm(monkeypatch, reply: str = "Okay.") -> dict:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": reply}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("local_coder.app_service.ensure_server", lambda wait_s=90: {"ok": True})
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def test_the_local_model_is_told_the_files_and_that_they_are_not_in_the_folder_yet(calc, monkeypatch):
    _fix(calc)
    captured = _install_fake_llm(monkeypatch, "The change is waiting for you to Keep.")
    result = chat_request(calc, QUESTION)
    messages = captured["json"]["messages"]
    assert messages[-1]["content"].startswith(QUESTION)
    assert "src/calc.py" in messages[-1]["content"]
    assert NOT_IN_FOLDER in messages[-1]["content"]
    assert "Keep or Discard" in messages[-1]["content"]
    assert str(data_root()) not in json.dumps(messages), "no tools, so no path to read"
    assert [turn["text"] for turn in load_chat(calc)["turns"][-2:]] == [QUESTION, result["summary"]]
    assert NOT_IN_FOLDER not in json.dumps(load_chat(calc)["turns"])


def test_the_local_note_follows_the_state_too(calc, monkeypatch):
    captured = _install_fake_llm(monkeypatch)
    chat_request(calc, QUESTION)
    assert captured["json"]["messages"][-1]["content"] == QUESTION
    _fix(calc, "req-1")
    chat_request(calc, QUESTION)
    assert NOT_IN_FOLDER in captured["json"]["messages"][-1]["content"]
    keep_changes("req-1", calc)
    chat_request(calc, QUESTION)
    assert captured["json"]["messages"][-1]["content"] == QUESTION
