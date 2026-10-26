"""Short commands Nicoder carries out itself: "just show me" runs its preview, "keep it" keeps the waiting result.

Nico's chat (2026-10-26, France project): after a build that passed its checks he wrote "just do it for me",
"just show me", "its not running" and "it worked and it stopped". Each became a build run; the model claimed it had
kept the change and started the app (one step, no tool call), or started a server in its sandbox that ended with
the run. Neither the model nor its sandbox can do these; Nicoder can.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from local_coder.app_rpc import RpcServer
from local_coder.app_service import keep_waiting, show_app
from local_coder.general import coordinator
from local_coder.general.chat_log import load_chat
from local_coder.general.inspect import classify_request

KEEP_HINT = ("The build passed and the app runs.\n\n"
             "It runs in the panel on the right; press Keep to put it in your folder.")


@pytest.mark.parametrize(("text", "kind"), [
    ("keep it", "code"),  # nothing waits: the words go on as any other message
    ("apply that", "code"),
    ("just show me", "show"),
    ("its not running", "show"),
    ("it's not running", "show"),
    ("it worked and it stopped", "show"),
    ("show me the app", "show"),
    ("run it", "show"),
    ("open it", "show"),
    ("can i see it?", "show"),
    ("did it run or not?", "chat"),
    ("do that", "code"),
    ("it doesnt work", "code"),
    ("keep going", "code"),
    ("start again", "code"),
    ("run the tests", "code"),
    ("fix the header", "code"),
    ("the page is blank", "code"),
])
def test_his_messages_go_where_they_can_be_done(text, kind):
    assert classify_request(text, {"supported": True})["kind"] == kind


@pytest.mark.parametrize(("text", "kind"), [
    ("keep it", "keep"),
    ("Keep", "keep"),
    ("keep the changes", "keep"),
    ("ok keep it in my project", "keep"),
    ("apply the changes", "code"),
    ("accept", "code"),
    ("keep going", "code"),
])
def test_keep_words_keep_only_while_a_result_waits(text, kind):
    assert classify_request(text, {"supported": True}, waiting=True)["kind"] == kind


def test_just_do_it_keeps_only_right_after_a_reply_that_said_to_keep():
    ready = {"supported": True}
    assert classify_request("just do it for me", ready, waiting=True)["kind"] == "code"
    verdict = classify_request("just do it for me", ready, last_reply=KEEP_HINT, waiting=True)
    assert verdict["kind"] == "keep" and verdict["show"] is True
    assert classify_request("just do it for me", ready, last_reply=KEEP_HINT)["kind"] == "code"  # nothing waits
    for reply in (
        "It builds.\n\nI can add a launchd agent so it runs at login; say the word and I'll wire it in.",
        "I'd keep the existing colours and only change the font. Say \"do it\" and I'll make the change.",
        "Want me to keep the old login page?",
        "I'd keep it simple: one page.",
    ):
        for yes in ("yes", "do it", "go ahead", "just do it for me"):
            assert classify_request(yes, ready, last_reply=reply, waiting=True)["kind"] == "code", (reply, yes)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "web"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"web"}\n', encoding="utf-8")
    return root


def test_show_me_runs_the_preview_and_says_where(project, monkeypatch):
    monkeypatch.setattr("local_coder.general.preview_copy.preview_project",
                        lambda folder: {"ok": True, "url": "http://127.0.0.1:4539/", "source": "waiting"})
    result = show_app(project, "just show me")
    assert result["preview"]["url"] == "http://127.0.0.1:4539/"
    assert result["summary"] == "It's running in the panel on the right. This is the version that waits for Keep."
    assert [turn["text"] for turn in load_chat(project)["turns"]] == ["just show me", result["summary"]]


def test_show_me_says_plainly_when_the_app_does_not_start(project, monkeypatch):
    refused = {"ok": False, "code": "no_package", "detail": "This project has no package.json yet."}
    monkeypatch.setattr("local_coder.general.preview_copy.preview_project", lambda folder: refused)
    assert show_app(project, "show me")["summary"] == "The app did not start: This project has no package.json yet."


def test_show_me_waits_while_a_change_elsewhere_uses_the_preview(project, monkeypatch):
    monkeypatch.setattr("local_coder.general.preview_copy.preview_project",
                        lambda folder: pytest.fail("the running change's preview must not be stopped"))
    with coordinator._request_running("other-project", "req-1"):
        result = show_app(project, "just show me")
    assert result["preview"] is None and "another project" in result["summary"]


def _change_a(candidate: Path, _text: str, _cancel) -> None:
    (candidate / "src" / "a.ts").write_text("export const a = 2\n", encoding="utf-8")


def test_keep_it_keeps_the_waiting_result_like_the_button(project):
    coordinator.run_general_request(project, "change a", request_id="req-1", fake_engine=_change_a,
                                    start_preview_after=False)
    assert (project / "src" / "a.ts").read_text() == "export const a = 1\n"  # waits for Keep

    result = keep_waiting(project, "keep it")

    assert result["episode_id"] == "req-1"
    assert result["summary"] == "Kept “change a” (1 file): the changes are in your project now."
    assert (project / "src" / "a.ts").read_text() == "export const a = 2\n"
    again = keep_waiting(project, "keep it")
    assert again["episode_id"] is None and again["summary"].startswith("Nothing is waiting to be kept")
    assert [turn["role"] for turn in load_chat(project)["turns"]][-4:] == ["user", "assistant", "user", "assistant"]


def test_the_app_asks_with_the_last_reply_and_calls_both(project, monkeypatch):
    monkeypatch.setattr("local_coder.general.preview_copy.preview_project",
                        lambda folder: {"ok": True, "url": "http://127.0.0.1:4539/", "source": "copy"})
    server = RpcServer(out=io.StringIO())

    def call(method, **params):
        return server.dispatch({"id": "1", "method": method, "params": params})["result"]

    coordinator.run_general_request(project, "change a", request_id="req-1", fake_engine=_change_a,
                                    start_preview_after=False)
    verdict = call("classify_request", path=str(project), text="just do it for me", last_reply=KEEP_HINT)
    assert verdict["kind"] == "keep" and verdict["show"] is True
    assert call("show_app", path=str(project), text="show me")["preview"]["url"] == "http://127.0.0.1:4539/"
    assert call("keep_waiting", path=str(project), text="keep it")["episode_id"] == "req-1"
    assert call("keep_waiting", path=str(project), text="keep it")["summary"].startswith("Nothing is waiting")


def test_a_show_after_keep_adds_only_its_reply(project, monkeypatch):
    monkeypatch.setattr("local_coder.general.preview_copy.preview_project",
                        lambda folder: {"ok": True, "url": "http://127.0.0.1:4539/", "source": "copy"})
    show_app(project, "")
    assert [(turn["role"], turn["text"]) for turn in load_chat(project)["turns"]] == [
        ("assistant", "It's running in the panel on the right."),
    ]


def test_keep_it_waits_while_a_change_is_being_made_here(project):
    coordinator.run_general_request(project, "change a", request_id="req-1", fake_engine=_change_a,
                                    start_preview_after=False)
    with coordinator._request_running(coordinator.project_id_for(project), "req-2"):
        result = keep_waiting(project, "keep it")
    assert result["episode_id"] is None and result["summary"].startswith("Keep waits")
    assert (project / "src" / "a.ts").read_text() == "export const a = 1\n"


def test_keep_it_names_the_older_result_it_keeps(project):
    coordinator.run_general_request(project, "change a", request_id="req-1", fake_engine=_change_a,
                                    start_preview_after=False)
    coordinator.run_general_request(project, "look around", request_id="req-2",
                                    fake_engine=lambda candidate, text, cancel: None, start_preview_after=False)
    result = keep_waiting(project, "keep it")
    assert result["episode_id"] == "req-1" and "“change a”" in result["summary"]


def test_the_app_classifies_keep_only_when_a_result_waits(project):
    server = RpcServer(out=io.StringIO())

    def kind(text):
        return server.dispatch({"id": "1", "method": "classify_request",
                                "params": {"path": str(project), "text": text}})["result"]["kind"]

    assert kind("keep it") == "code"
    coordinator.run_general_request(project, "change a", request_id="req-1", fake_engine=_change_a,
                                    start_preview_after=False)
    assert kind("keep it") == "keep"


def test_a_damaged_record_never_blocks_a_message(project, monkeypatch):
    def broken(_folder):
        raise ValueError("created_ts is not a number")

    monkeypatch.setattr("local_coder.general.waiting.find_waiting_result", broken)
    server = RpcServer(out=io.StringIO())

    def kind(text):
        return server.dispatch({"id": "1", "method": "classify_request",
                                "params": {"path": str(project), "text": text}})["result"]["kind"]

    assert kind("keep it") == "code"
    assert kind("add a footer") == "code"
