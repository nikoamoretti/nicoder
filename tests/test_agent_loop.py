"""The agent loop with a scripted model: tool rounds, limits, Stop, and room management."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from local_coder.agent.access import AccessPolicy
from local_coder.agent.loop import DROPPED, AgentSession
from local_coder.agent.tools import ToolBox
from local_coder.hosted import ReplyCutOff


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


class Dropping(Script):
    """A model whose first `drops` replies the provider cuts off partway."""

    def __init__(self, drops: int, *replies: dict) -> None:
        super().__init__(*replies)
        self.drops = drops

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict:
        if self.drops:
            self.drops -= 1
            self.sent.append(json.loads(json.dumps(messages)))
            raise ReplyCutOff("OrcaRouter closed the connection partway through the reply (RemoteProtocolError)")
        return super().__call__(messages, tools)


def test_a_reply_dropped_partway_asks_for_the_work_in_parts(home):
    activity: list[str] = []
    script = Dropping(1, _reply(calls=[_call("read", {"path": "~/projects/site/README.md"}, "c1")]), _reply("Done."))
    run = _session(home, script, on_activity=activity.append).ask("go")
    assert (run.status, run.answer) == ("answered", "Done.")
    assert script.sent[1][-1]["content"].startswith("Your last reply was lost")
    assert activity == ["The connection dropped during a long reply; asking for it in parts",
                        "Reading ~/projects/site/README.md"]


def test_replies_dropped_again_and_again_end_the_run(home):
    run = _session(home, Dropping(4)).ask("go")
    assert run.status == "error" and "closed the connection partway" in run.answer


def test_a_call_cut_off_at_the_output_limit_says_so(home):
    cut = _reply(calls=[_call("read", '{"path": "~/projects/site/REA', "c1")])
    cut["choices"][0]["finish_reason"] = "length"
    script = Script(cut, _reply("Done."))
    run = _session(home, script).ask("go")
    assert run.status == "answered"
    assert script.sent[1][-1]["content"].startswith("Your reply hit this model's output limit and was cut off")


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


def test_hurry_note_is_sent_once_halfway(home):
    loop_call = _call("list", {"path": "~"}, "c")
    script = Script(*[_reply(calls=[{**loop_call, "id": f"c{n}"}]) for n in range(3)], _reply("Answer."))
    session = _session(home, script)
    run = session.ask("go", max_steps=6, hurry_after=0.5)
    assert run.answer == "Answer."
    hurry = [row for row in session.messages if row["role"] == "user" and "budget" in row["content"]]
    assert len(hurry) == 1
    assert len(session.calls_log) == 4 and all("seconds" in row for row in session.calls_log)


def test_an_answer_with_nothing_checked_is_sent_back_once_to_look_first():
    # Nico's France chat (2026-10-26): "Let me check what's on disk" and "nothing is there yet" with nothing read.
    from local_coder.agent.loop import _LOOK_FIRST, AgentSession
    from local_coder.agent.tools import AccessPolicy, ToolBox

    seen: list[list[dict]] = []
    answers = iter(["Let me check what's actually on disk right now.", "Your folder has src/ and package.json."])

    def chat(messages, tools):
        seen.append(list(messages))
        return {"choices": [{"message": {"role": "assistant", "content": next(answers)}}]}

    import tempfile
    from pathlib import Path

    home = Path(tempfile.mkdtemp())
    box = ToolBox(AccessPolicy(home=home, project=home), cwd=home, run_tmp=home / "tmp")
    session = AgentSession(chat=chat, toolbox=box, system="s")
    run = session.ask("did you check", look_first=True)
    assert run.answer == "Your folder has src/ and package.json."
    assert seen[1][-1] == {"role": "user", "content": _LOOK_FIRST}
    plain = AgentSession(chat=lambda m, t: {"choices": [{"message": {"role": "assistant", "content": "Paris."}}]},
                         toolbox=ToolBox(AccessPolicy(home=home, project=None), cwd=home, run_tmp=home / "tmp"),
                         system="s")
    assert plain.ask("capital of France?").answer == "Paris."  # no project: answered at once


def test_a_plain_answer_stands_when_asked_to_check_it_still_checks_nothing():
    import tempfile
    from pathlib import Path

    from local_coder.agent.loop import AgentSession
    from local_coder.agent.tools import AccessPolicy, ToolBox

    home = Path(tempfile.mkdtemp())
    answers = iter(["It is a Vite app with one page.", "Understood, I'll check."])
    calls: list[int] = []

    def chat(messages, tools):
        calls.append(1)
        return {"choices": [{"message": {"role": "assistant", "content": next(answers)}}]}

    box = ToolBox(AccessPolicy(home=home, project=home), cwd=home, run_tmp=home / "tmp")
    assert AgentSession(chat=chat, toolbox=box, system="s").ask("what is this?", look_first=True).answer == (
        "It is a Vite app with one page."
    )
    calls.clear()
    def welcome(messages, tools):
        calls.append(1)
        return {"choices": [{"message": {"content": "You're welcome."}}]}

    thanks = AgentSession(chat=welcome, toolbox=box, system="s")
    assert thanks.ask("thanks", look_first=True).answer == "You're welcome." and len(calls) == 1


@pytest.mark.parametrize("promise", [
    "Sure, let me look.", "Let's check the folder.", "I’ll check now.", "Now let me see.",
])
def test_a_promise_never_stands_as_the_answer(promise):
    from local_coder.agent.loop import _PROMISE

    assert _PROMISE.match(promise)


def _change_session(home: Path, script: Script) -> tuple[AgentSession, Path]:
    work = home / ".local" / "share" / "lc" / "work"
    work.mkdir(parents=True)
    (work / "README.md").write_text("# Site\nA tiny site.\n", encoding="utf-8")
    box = ToolBox(AccessPolicy(home=home, workdir=work, project=home / "projects" / "site"), cwd=work,
                  run_tmp=home.parent / "tmp")
    return AgentSession(chat=script, toolbox=box, system="system prompt"), work


def _step_backs(sent: list[dict]) -> list[str]:
    return [str(row["content"]) for row in sent if row["role"] == "user" and "without changing a file" in
            str(row["content"])]


def test_a_change_run_that_stops_changing_files_is_asked_to_step_back(home):
    # 2026-10-27 benchmark: two runs edited early, then spent 30 steps on throwaway debug scripts.
    looks = [_reply(calls=[_call("list", {"path": "."}, f"l{n}")]) for n in range(10)]
    write = _reply(calls=[_call("write", {"path": "app.py", "content": "x = 1\n"}, "w")])
    fix = _reply("The loop runs from the wrong end.", [_call("write", {"path": "app.py", "content": "x = 2\n"}, "f")])
    script = Script(write, *looks, fix, _reply("Done."))
    session, _ = _change_session(home, script)
    assert session.ask("go", max_steps=30).status == "answered"
    assert script.sent[11][-1]["content"].startswith("You have taken 10 steps without changing a file.")
    assert not _step_backs(script.sent[10])  # not before the tenth step
    assert len(_step_backs(script.sent[-1])) == 1


def test_a_reply_with_no_tool_right_after_the_note_is_sent_back_once(home):
    looks = [_reply(calls=[_call("list", {"path": "."}, f"l{n}")]) for n in range(10)]
    script = Script(_reply(calls=[_call("write", {"path": "app.py", "content": "x = 1\n"}, "w")]), *looks,
                    _reply("The loop runs from the wrong end."), _reply("Fixed the loop; the tests pass."))
    session, _ = _change_session(home, script)
    assert session.ask("go", max_steps=30).answer == "Fixed the loop; the tests pass."
    assert script.sent[-1][-1] == {"role": "user", "content": "Go ahead and do that now with your tools."}


def test_a_command_that_changes_a_file_counts_as_progress(home):
    looks = [_reply(calls=[_call("list", {"path": "."}, f"l{n}")]) for n in range(10)]
    script = Script(_reply(calls=[_call("write", {"path": "app.py", "content": "x = 1\n"}, "w")]), *looks,
                    _reply("Done."))
    session, work = _change_session(home, script)
    replay = script.__call__

    def chat(messages, tools):
        if len(script.sent) == 5:
            (work / "app.py").write_text("x = 22\n", encoding="utf-8")  # as a bash command would
        return replay(messages, tools)

    session.chat = chat
    session.ask("go", max_steps=30)
    assert not _step_backs(script.sent[-1])


def test_looking_around_before_the_first_change_is_not_stepping_back(home):
    looks = [_reply(calls=[_call("list", {"path": "."}, f"l{n}")]) for n in range(12)]
    script = Script(*looks, _reply("The site has one README."))
    session, _ = _change_session(home, script)
    session.ask("go", max_steps=30)
    assert not _step_backs(script.sent[-1])


def test_a_step_announced_without_a_tool_is_sent_back_once(home):
    # 2026-10-27 replay: "The bug: … Rewriting more simply:" and no tool call ended the run with the fix unwritten.
    script = Script(_reply("The loop runs the wrong way. Rewriting more simply:"),
                    _reply("Rewrote it; the tests pass."))
    session = _session(home, script)
    run = session.ask("go")
    assert run.status == "answered" and run.answer == "Rewrote it; the tests pass." and run.steps == 2
    assert script.sent[-1][-1] == {"role": "user", "content": "Go ahead and do that now with your tools."}
    again = Script(_reply("Next:"), _reply("Still:"))
    assert _session(home, again).ask("go").answer == "Still:"  # once only


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_a_command_shows_while_it_runs_and_its_end_takes_its_place(home):
    # Nico, 2026-10-27: a command's step appeared once it ended, so a long one left the step before it on screen.
    steps: list[dict] = []
    bash = _call("bash", {"command": "ls", "description": "List the project"}, "b")
    script = Script(_reply(calls=[bash, _call("read", {"path": "projects/site/README.md"}, "r")]), _reply("Done."))
    session = _session(home, script, on_step=steps.append)
    assert session.ask("go").status == "answered"
    start, end, read = steps
    assert start["running"] and start["target"] == "List the project" and start["call"] == end["call"]
    assert "running" not in end and end["target"] == "List the project" and end["command"] == "ls" and end["ok"]
    assert read["tool"] == "read" and "running" not in read and read["call"] != end["call"]


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_a_command_on_the_mac_says_so_while_it_runs_and_when_it_ends(home, tmp_path):
    work = home / "wc"
    work.mkdir()
    box = ToolBox(AccessPolicy(home=home, workdir=work, project=home / "projects" / "site"), cwd=work,
                  run_tmp=tmp_path / "tmp", mac_access=True)
    steps: list[dict] = []
    bash = _call("bash", {"command": "mkdir -p made", "description": "Make a folder", "on_mac": True}, "m")
    session = AgentSession(chat=Script(_reply(calls=[bash]), _reply("Done.")), toolbox=box, system="system prompt",
                           on_step=steps.append)
    assert session.ask("go").status == "answered"
    start, end = steps
    assert start["running"] and start["mac"] and end["mac"] and end["ok"] and "running" not in end
    assert (work / "made").is_dir()
