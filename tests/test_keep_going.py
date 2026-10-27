"""A run that runs out of time or steps while still changing files carries on by itself (up to two hours).

Nico (2026-10-27): "it keeps telling me that after 30 minutes of working I ran out of time before finishing. Say
continue…" Six runs of his kebab project stopped at 30 minutes: one waiting on OpenStreetMap queries, the others
doing real work, 130 small steps in the last one.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from local_coder.general import coordinator
from local_coder.general.budgets import budget_for
from local_coder.general.snapshot import index_tree


@dataclass
class Run:
    answer: str
    status: str
    steps: int = 1
    tool_calls: int = 0


class Session:
    """Scripted turns; each one may change the working copy first."""

    def __init__(self, workdir: Path, turns: list[tuple[str, str, bool]]) -> None:
        self.workdir = workdir
        self.turns = turns
        self.asked: list[str] = []

    def ask(self, text, *, max_steps, deadline, **_):
        self.asked.append(text)
        answer, status, changes = self.turns.pop(0)
        if changes:
            (self.workdir / f"part{len(self.asked)}.ts").write_text("export {}\n", encoding="utf-8")
        return Run(answer, status)


def _setup(tmp_path, turns):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    window = index_tree(work)
    (work / "b.ts").write_text("export const b = 2\n", encoding="utf-8")  # the first stretch changed a file
    events: list[dict] = []
    ledger = coordinator._GenerationLedger(budget_for("create"))
    return work, window, Session(work, turns), ledger, events


def test_a_run_that_still_changes_files_carries_on_until_it_answers(tmp_path):
    turns = [("halfway", "time_limit", True), ("Done.", "answered", True)]
    work, window, session, ledger, events = _setup(tmp_path, turns)
    first = Run("I built the importer; the page is next.", "time_limit")
    run = coordinator._keep_going(session, first, ledger, work, window, events.append, threading.Event())
    assert run.status == "answered" and run.answer == "Done."
    assert session.asked == [coordinator._KEEP_GOING, coordinator._KEEP_GOING]
    assert any("Still working" in str(event.get("data", {}).get("detail")) for event in events)


def test_a_stretch_that_changed_nothing_ends_with_its_own_account(tmp_path):
    work, _, session, ledger, events = _setup(tmp_path, [])
    unchanged = index_tree(work)
    first = Run("I looked around but changed nothing.", "time_limit")
    run = coordinator._keep_going(session, first, ledger, work, unchanged, events.append, threading.Event())
    assert run is first and session.asked == []


def test_two_hours_in_all_or_stop_ends_it(tmp_path):
    work, window, session, ledger, events = _setup(tmp_path, [("more", "time_limit", True)])
    ledger.started -= coordinator.AGENT_MAX_WALL_S - 60
    first = Run("Still going.", "time_limit")
    assert coordinator._keep_going(session, first, ledger, work, window, events.append, threading.Event()) is first
    stop = threading.Event()
    stop.set()
    fresh = coordinator._GenerationLedger(budget_for("create"))
    assert coordinator._keep_going(session, first, fresh, work, window, events.append, stop) is first
    assert session.asked == []


def test_each_stretch_gets_half_an_hour_more(tmp_path):
    work, window, session, ledger, events = _setup(tmp_path, [("Done.", "answered", False)])
    ledger.started -= ledger.wall_seconds  # this stretch used all of its time
    coordinator._keep_going(session, Run("halfway", "time_limit"), ledger, work, window, events.append,
                            threading.Event())
    assert 1700 < ledger.remaining_s() <= coordinator.AGENT_MORE_S


def test_a_real_request_out_of_steps_carries_on_and_ends_with_its_answer(tmp_path, monkeypatch):
    import os

    from local_coder.general.coordinator import load_meta, run_general_request

    from .test_agent_app import Script, _call, _reply

    home = Path(os.path.realpath(tmp_path / "home"))
    folder = home / "projects" / "kebabs"
    folder.mkdir(parents=True)
    (folder / "pyproject.toml").write_text('[project]\nname = "kebabs"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(coordinator, "AGENT_MAX_STEPS", 3)
    writes = [_reply(calls=[_call("write", {"path": f"kebabs/part{n}.py", "content": f"N = {n}\n"}, str(n))])
              for n in range(4)]
    # Out of steps after three writes, the model gives its account (the wrap-up), then carries on by itself.
    script = Script(*writes[:3], _reply("Three parts are written; one to go."), writes[3],
                    _reply("All four parts are written and the list builds."))
    events: list[dict] = []
    review = run_general_request(folder, "List every kebab shop in France", agent_chat=script,
                                 start_preview_after=False, listener=events.append)
    assert review["summary"] == "All four parts are written and the list builds."
    assert sorted(review["facts"]["files"]) == [f"kebabs/part{n}.py" for n in range(4)]
    assert any("Still working" in str((event.get("data") or {}).get("detail")) for event in events)
    assert load_meta(review["project_id"], review["request_id"]).get("stop_reason") != "step_limit"


def test_carrying_on_stops_at_three_dollars(tmp_path):
    work, window, session, ledger, events = _setup(tmp_path, [("more", "time_limit", True)])
    session.cost_usd = coordinator.AGENT_KEEP_GOING_MAX_USD
    first = Run("Halfway through the list.", "time_limit")
    assert coordinator._keep_going(session, first, ledger, work, window, events.append, threading.Event()) is first
    assert session.asked == []
    session.cost_usd = 1.2
    session.turns = [("Done.", "answered", True)]
    coordinator._keep_going(session, first, ledger, work, window, events.append, threading.Event())
    assert any("$1.20" in str((event.get("data") or {}).get("detail")) for event in events)


def test_limits_the_caller_set_are_kept(tmp_path):
    work, window, session, _, events = _setup(tmp_path, [("more", "time_limit", True)])
    fixed = coordinator._GenerationLedger(budget_for("create"), generation_limit=1)
    first = Run("One step done.", "step_limit")
    assert coordinator._keep_going(session, first, fixed, work, window, events.append, threading.Event()) is first
    assert session.asked == []
