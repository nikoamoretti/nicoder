"""The agent loop with a scripted model: tool rounds, limits, Stop, and room management."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from local_coder.agent.access import AccessPolicy
from local_coder.agent.loop import DROPPED, AgentSession
from local_coder.agent.tools import ToolBox


def _call(name: str, args: dict | str, call_id: str | None = None) -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args)
    call = {"type": "function", "function": {"name": name, "arguments": arguments}}
    if call_id:
        call["id"] = call_id
    return call


def _reply(content: str = "", calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Script:
    """A fake model that replays replies and records what it was sent."""

    def __init__(self, *replies: dict) -> None:
        self.replies = list(replies)
        self.sent: list[list[dict]] = []
        self.tools: list[list[dict]] = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        self.sent.append(json.loads(json.dumps(messages)))
        self.tools.append(tools)
        if not self.replies:
            raise AssertionError("the script ran out of replies")
        return self.replies.pop(0)


@pytest.fixture
def home(tmp_path) -> Path:
    root = Path(os.path.realpath(tmp_path / "home"))
    (root / "projects" / "site").mkdir(parents=True)
    (root / "projects" / "site" / "README.md").write_text("# Site\nA tiny site.\n", encoding="utf-8")
    return root


def _session(home: Path, script: Script, **kwargs) -> AgentSession:
    box = ToolBox(AccessPolicy(home=home), cwd=home, run_tmp=home.parent / "tmp")
    return AgentSession(chat=script, toolbox=box, system="system prompt", **kwargs)


def test_tool_round_then_answer(home):
    script = Script(
        _reply("Looking.", [_call("read", {"path": "~/projects/site/README.md"}, "c1")]),
        _reply("It is a tiny site."),
    )
    activity: list[str] = []
    session = _session(home, script, on_activity=activity.append, history=[{"role": "user", "content": "hi"}])
    run = session.ask("What is the site?")
    assert (run.status, run.answer, run.steps, run.tool_calls) == ("answered", "It is a tiny site.", 2, 1)
    assert activity == ["Looking.", "Reading ~/projects/site/README.md"]
    second = script.sent[1]
    assert [row["role"] for row in second] == ["system", "user", "user", "assistant", "tool"]
    assert second[-1]["tool_call_id"] == "c1" and "A tiny site." in second[-1]["content"]
    assert session.toolbox.read_paths == ["~/projects/site/README.md"]


def test_missing_call_ids_and_bad_arguments_are_handled(home):
    script = Script(_reply(calls=[_call("read", "{oops")]), _reply("Done."))
    run = _session(home, script).ask("go")
    tool_message = script.sent[1][-1]
    assert tool_message["tool_call_id"] == "call_1"
    assert "not valid JSON" in tool_message["content"]
    assert run.status == "answered"


def test_empty_answer_gets_one_nudge(home):
    script = Script(_reply(""), _reply("Here is the answer."))
    run = _session(home, script).ask("go")
    assert run.answer == "Here is the answer."
    assert script.sent[1][-1]["content"].startswith("Please answer the user now")


def test_step_limit_asks_for_a_summary_and_runs_no_more_tools(home):
    loop_call = _call("list", {"path": "~"}, "c1")
    script = Script(
        _reply(calls=[loop_call]),
        _reply(calls=[{**loop_call, "id": "c2"}]),
        _reply("I looked at the home folder twice.", [{**loop_call, "id": "c3"}]),
    )
    session = _session(home, script)
    run = session.ask("go", max_steps=2)
    assert run.status == "step_limit"
    assert run.answer == "I looked at the home folder twice."
    assert script.sent[-1][-1]["content"].startswith("Stop using tools now")
    assert session.messages[-1] == {"role": "assistant", "content": "I looked at the home folder twice."}


def test_stop_skips_the_remaining_tools(home):
    cancel = threading.Event()

    def chat(messages, tools):
        cancel.set()
        return _reply(calls=[_call("list", {"path": "~"}, "a"), _call("list", {"path": "~"}, "b")])

    session = _session(home, Script(), cancel=cancel)
    session.chat = chat
    run = session.ask("go")
    assert run.status == "stopped"
    results = [row for row in session.messages if row["role"] == "tool"]
    assert [row["content"] for row in results] == ["Stopped by the user before this tool ran."] * 2


def test_model_failure_ends_the_request(home):
    def broken(messages, tools):
        raise RuntimeError("HTTP 401")

    session = _session(home, Script())
    session.chat = broken
    run = session.ask("go")
    assert run.status == "error" and "HTTP 401" in run.answer


def test_oldest_tool_output_is_dropped_first(home):
    big = "x" * 5000
    session = _session(home, Script(), context_chars=9000)
    session.messages += [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [_call("read", {}, "1")]},
        {"role": "tool", "tool_call_id": "1", "content": big},
        {"role": "assistant", "content": "", "tool_calls": [_call("read", {}, "2")]},
        {"role": "tool", "tool_call_id": "2", "content": big},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "user", "content": "d"},
    ]
    session._compact()
    tools = [row["content"] for row in session.messages if row["role"] == "tool"]
    assert tools[0] == DROPPED
    assert tools[1] == big


def test_transcript_is_saved(home, tmp_path):
    session = _session(home, Script(_reply("ok")))
    session.ask("go")
    saved = json.loads(session.save(tmp_path / "t.json", status="answered").read_text())
    assert saved["status"] == "answered"
    assert saved["messages"][-1] == {"role": "assistant", "content": "ok"}


def test_headings_and_tables_become_chat_friendly():
    from local_coder.agent.loop import plain_answer

    text = (
        "## Where it lives\n\nThe repo is `~/x`.\n\n"
        "| File | Job |\n|------|-----|\n| `a.ts` | Polls feeds |\n| `b.ts` | Scores |\n\nDone."
    )
    assert plain_answer(text) == (
        "**Where it lives**\n\nThe repo is `~/x`.\n\n- `a.ts` — Polls feeds\n- `b.ts` — Scores\n\nDone."
    )
    assert plain_answer("Plain | text with a pipe") == "Plain | text with a pipe"


def test_dominant_project_needs_a_clear_majority(tmp_path):
    from local_coder.agent.runner import dominant_project

    home = Path(os.path.realpath(tmp_path))
    repo = home / "projects" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "lib" / "deep").mkdir(parents=True)
    other = home / "projects" / "other"
    other.mkdir(parents=True)
    (other / "package.json").write_text("{}")
    files = [repo / "lib" / "deep" / f"{name}.ts" for name in "abc"]
    assert dominant_project(files, home) == repo
    assert dominant_project(files[:2], home) is None
    assert dominant_project(files + [other / f"{name}.ts" for name in "defg"], home) is None


def test_hurry_note_is_sent_once_halfway(home):
    loop_call = _call("list", {"path": "~"}, "c")
    script = Script(*[_reply(calls=[{**loop_call, "id": f"c{n}"}]) for n in range(3)], _reply("Answer."))
    session = _session(home, script)
    run = session.ask("go", max_steps=6, hurry_after=0.5)
    assert run.answer == "Answer."
    hurry = [row for row in session.messages if row["role"] == "user" and "budget" in row["content"]]
    assert len(hurry) == 1
    assert len(session.calls_log) == 4 and all("seconds" in row for row in session.calls_log)
