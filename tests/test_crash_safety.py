"""A.1/A.2 recording and crash-budget contract.

Slots are reserved before dispatch; dispatched-but-unknown requests keep
their slot; resumes reconcile without resampling or double-execution; the
ninth generation stays impossible across crashes; elapsed budgets persist.
"""
import json

import pytest

from local_coder.inference.fake import ScriptedFakeAdapter, demo_script
from local_coder.persistence import EpisodeStore

from .conftest import make_controller


class CrashError(Exception):
    """Simulated process crash: bypasses AdapterError handling."""


class CrashingFake(ScriptedFakeAdapter):
    """Raises CrashError on the configured call index (1-based)."""

    def __init__(self, script, crash_on=1):
        super().__init__(list(script), "crash-test")
        self.crash_on = crash_on

    def generate(self, briefing, step):
        self.calls += 1
        self.last_request = {"adapter": "fake", "profile": self.profile}
        self.last_usage = None
        if self.calls == self.crash_on:
            raise CrashError("simulated crash during dispatch")
        return self.script[step]


def _turn(store, episode, turn):
    directory = store.turn_dir(episode, turn)
    assert directory.exists(), f"turn bundle {turn} missing"
    return {p.name: p.read_text(encoding="utf-8") for p in directory.iterdir() if p.is_file()}


def test_first_briefing_retrievable_and_hash_valid_after_restart(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:success"),
                                    episode_id="ep-turns")
    controller.run()
    store = EpisodeStore(state_dir)
    files = _turn(store, "ep-turns", 1)
    for name in ("state-pre.json", "briefing.txt", "profile.json", "request.json",
                 "response.json", "action.json", "observation.json", "state-post.json",
                 "manifest.json", "status.json"):
        assert name in files, name
    ok, bad = store.verify_turn("ep-turns", 1)
    assert ok, bad
    first_briefing = files["briefing.txt"]
    assert "TASK" in first_briefing and "RULES" in first_briefing
    # After a restart the same bytes verify.
    reloaded = EpisodeStore(state_dir)
    ok, bad = reloaded.verify_turn("ep-turns", 1)
    assert ok, bad
    assert reloaded.read_turn_file("ep-turns", 1, "briefing.txt") == first_briefing
    # Profile pins protocol identity and constraint per generation.
    profile = json.loads(files["profile.json"])
    assert profile["protocol_version"] == "1"
    assert profile["constraint"] == "json_object"


def test_crash_during_dispatch_consumes_slot_and_reconciles(repo_copy, state_dir):
    import json

    from .conftest import FIXTURE_DIR

    gold = (FIXTURE_DIR / "gold.diff").read_text()
    patch = json.dumps({"action": "patch", "operation": "apply", "diff": gold,
                        "expected_effect": "add() returns the sum"})
    check = '{"action":"check","scope":"focused"}'
    done = '{"action":"done","status":"solved","summary":"Fixed add()."}'
    find = demo_script("fake:success")[0]
    controller, _ = make_controller(repo_copy, state_dir, [], episode_id="ep-crash1")
    controller.adapter = CrashingFake([find, patch, check, done], crash_on=2)
    controller.start()
    assert controller.step() is True  # slot 1: FIND ok
    with pytest.raises(CrashError):
        controller.step()  # slot 2: PATCH dispatch crashes
    assert controller.state.generations_used == 2
    store = controller.store
    assert (store.turn_dir("ep-crash1", 2) / "status.json").exists()
    assert not (store.turn_dir("ep-crash1", 2) / "response.json").exists()
    # Resume continues at slot 3 with an aligned script; slot 2 is reconciled.
    controller2, adapter2 = make_controller(
        repo_copy, state_dir, [find, find, patch, check, done], episode_id="ep-crash1")
    final = controller2.resume("ep-crash1")
    assert adapter2.calls == 3  # slots 3..5 only; slot 2 never resampled
    assert final.generations_used == 5
    assert final.outcome == "solved"
    events = controller2.store.read_events("ep-crash1")
    reconciled = [e for e in events if e["type"] == "reconciled"
                  and e["data"].get("result") == "interrupted-unknown-outcome"]
    assert reconciled, "crashed dispatch must be reconciled, not replayed"


def test_crash_after_response_never_executes_twice(repo_copy, state_dir):
    import json

    from .conftest import FIXTURE_DIR

    gold = (FIXTURE_DIR / "gold.diff").read_text()
    patch = json.dumps({"action": "patch", "operation": "apply", "diff": gold,
                        "expected_effect": "add() returns the sum"})
    check = '{"action":"check","scope":"focused"}'
    done = '{"action":"done","status":"solved","summary":"Fixed add()."}'
    find = demo_script("fake:success")[0]
    controller, _ = make_controller(repo_copy, state_dir, [find, patch, check, done],
                                    episode_id="ep-crash2")
    controller.start()
    controller.step()  # FIND ok
    real_patch = controller._do_patch

    def boom(action):
        raise CrashError("simulated crash inside skill")

    controller._do_patch = boom
    with pytest.raises(CrashError):
        controller.step()  # PATCH dispatched, response recorded, skill crashed
    store = controller.store
    assert (store.turn_dir("ep-crash2", 2) / "response.json").exists()
    assert not (store.turn_dir("ep-crash2", 2) / "observation.json").exists()
    controller._do_patch = real_patch
    # Resume re-drives its own PATCH (aligned script); the recorded crashed
    # response is marked, never executed.
    controller2, _ = make_controller(repo_copy, state_dir, [find, find, patch, check, done],
                                     episode_id="ep-crash2")
    final = controller2.resume("ep-crash2")
    assert final.outcome == "solved"
    events = controller2.store.read_events("ep-crash2")
    assert len([e for e in events if e["type"] == "patch_applied"]) == 1
    reconciled = [e for e in events if e["type"] == "reconciled"
                  and e["data"].get("result") == "response-without-observation"]
    assert reconciled


def test_double_resume_is_stable(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:wrong"),
                                    episode_id="ep-double")
    first = controller.run()
    assert first.outcome == "blocked"
    store = EpisodeStore(state_dir)
    turns_before = store.list_turns("ep-double")
    assert turns_before == [1, 2, 3, 4, 5]
    controller2, adapter2 = make_controller(repo_copy, state_dir, demo_script("fake:wrong"),
                                            episode_id="ep-double")
    second = controller2.resume("ep-double")
    assert second.outcome == "blocked"
    assert adapter2.calls == 0
    assert store.list_turns("ep-double") == turns_before
    for turn in turns_before:
        ok, bad = store.verify_turn("ep-double", turn)
        assert ok, (turn, bad)


def test_ninth_generation_impossible_across_resume(repo_copy, state_dir):
    script = [f'{{"action":"find","query":"distinct-query-{i}","symbols":[]}}' for i in range(9)]
    controller, adapter = make_controller(repo_copy, state_dir, script, episode_id="ep-nine")
    state = controller.run()
    assert state.generations_used == 8
    controller2, adapter2 = make_controller(repo_copy, state_dir, script, episode_id="ep-nine")
    resumed = controller2.resume("ep-nine")
    assert resumed.outcome == "blocked"
    assert adapter2.calls == 0
    assert resumed.generations_used == 8


def test_elapsed_budget_persists_across_resume(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:blocked"),
                                    episode_id="ep-elapsed")
    controller.start()
    controller.step()
    assert controller.state.elapsed_s >= 0.0
    before = controller.state.elapsed_s
    controller2, _ = make_controller(repo_copy, state_dir, demo_script("fake:blocked"),
                                     episode_id="ep-elapsed")
    resumed_state = controller2.store.load_state("ep-elapsed")
    assert resumed_state.elapsed_s == before
    # A nearly spent budget still binds after resume (no renewal).
    controller2.state = resumed_state
    controller2.workspace = controller.workspace
    controller2.original_manifest = controller.original_manifest
    controller2.repo = controller.repo
    controller2._tick = controller2.started_mono if hasattr(controller2, "started_mono") else 0.0
    import time as _time

    controller2.started_mono = _time.monotonic()
    controller2._tick = _time.monotonic()
    resumed_state.budgets.max_elapsed_s = 0
    resumed_state.elapsed_s = 0.0
    controller2.store.save_state(resumed_state)
    assert controller2.step() is False
    assert controller2.state.outcome == "blocked"


def test_terminal_repeat_turn_records_observation(repo_copy, state_dir):
    find = '{"action":"find","query":"same query","symbols":[]}'
    controller, _ = make_controller(repo_copy, state_dir, [find, find, find],
                                    episode_id="ep-repeat")
    final = controller.run()
    assert final.outcome == "blocked"
    store = controller.store
    assert store.list_turns("ep-repeat") == [1, 2, 3]
    for turn in (1, 2, 3):
        ok, bad = store.verify_turn("ep-repeat", turn)
        assert ok, (turn, bad)
    import json as _json

    obs = _json.loads(store.read_turn_file("ep-repeat", 3, "observation.json"))
    assert obs["status"] == "no_progress"
