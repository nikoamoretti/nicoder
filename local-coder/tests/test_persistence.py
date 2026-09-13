"""Evidence durability: atomic state, append-only events, resume."""
from local_coder.inference.fake import demo_script
from local_coder.persistence import EpisodeStore

from .conftest import make_controller


def test_state_roundtrip_and_event_continuity(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:success"),
                                    episode_id="ep-roundtrip")
    state = controller.run()
    assert state.outcome == "solved"
    store = EpisodeStore(state_dir)
    reloaded = store.load_state("ep-roundtrip")
    assert reloaded == state
    assert store.verify_events("ep-roundtrip") > 10
    # Failure evidence survives a restart byte-identical.
    assert reloaded.baseline.failed == state.baseline.failed
    assert reloaded.final.passed == state.final.passed


def test_resume_continues_without_double_apply(repo_copy, state_dir):
    controller, adapter = make_controller(repo_copy, state_dir, demo_script("fake:wrong"),
                                          episode_id="ep-resume")
    controller.start()
    assert controller.step() is True  # FIND
    used = controller.state.generations_used
    assert used == 1
    # A fresh controller resumes the same episode with the same script.
    controller2, adapter2 = make_controller(repo_copy, state_dir, demo_script("fake:wrong"),
                                            episode_id="ep-resume")
    final = controller2.resume("ep-resume")
    assert final.generations_used == 5
    assert adapter2.calls == 4  # resume consumed script[1..4], never replayed script[0]
    assert final.outcome == "blocked"
    assert len([p for p in [final.last_patch, *final.patch_history] if p]) == 1
    types = [e["type"] for e in controller2.store.read_events("ep-resume")]
    assert "resumed" in types
    assert types.count("patch_applied") == 1


def test_reconcile_marks_unapplied_patch_intent(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:success"),
                                    episode_id="ep-reconcile")
    controller.start()
    store = controller.store
    store.append_event("ep-reconcile", "patch_intent", {"diff_sha": "zzz", "base_tree": "yyy"})
    controller2, _ = make_controller(repo_copy, state_dir, demo_script("fake:blocked"),
                                     episode_id="ep-reconcile")
    final = controller2.resume("ep-reconcile")
    events = controller2.store.read_events("ep-reconcile")
    reconciled = [e for e in events if e["type"] == "reconciled"]
    assert reconciled, "expected a reconciliation marker, not a blind replay"
    assert final.outcome in ("blocked", "solved")
