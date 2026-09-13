"""Mandatory verification policy: edit -> automatic CHECK before next decision."""
import json

import pytest

from local_coder.state import PendingVerification

from .conftest import make_controller


class CrashError(Exception):
    """Simulated process crash that bypasses AdapterError handling."""

CORRECT_OLD = "    return a - b  # BUG: subtracts instead of adding"
CORRECT_NEW = "    return a + b"
WRONG_NEW = "    return a - b + 1"


def _replace_script(*edits):
    """Fake v2 script: find, one replace per edit-pair, check, done-solved."""
    script = ['{"action":"find","query":"add subtracts calc","symbols":["add"]}']
    for old, new in edits:
        script.append(json.dumps({"action": "patch", "operation": "replace",
                                  "edits": [{"file_id": 2, "old": old, "new": new}],
                                  "expected_effect": "fix add"}))
    script.append('{"action":"check","scope":"focused"}')
    script.append('{"action":"done","status":"solved","summary":"Fixed add()."}')
    return script


def _mandatory(repo_copy, state_dir, script, episode_id):
    return make_controller(repo_copy, state_dir, script, episode_id=episode_id,
                           protocol_version="2", verification_policy="mandatory")


def _auto_events(controller, episode_id):
    return [e for e in controller.store.read_events(episode_id) if e["type"].startswith("auto_check")]


def test_correct_edit_checked_before_next_decision(repo_copy, state_dir):
    controller, adapter = _mandatory(
        repo_copy, state_dir, _replace_script((CORRECT_OLD, CORRECT_NEW)), "ep-verify-ok")
    controller.start()
    assert controller.step() is True  # find
    assert controller.step() is True  # patch + automatic check, same turn
    state = controller.state
    assert state.auto_checks == 1
    assert state.pending_verification is None
    assert state.current is not None and state.current.status == "pass"
    assert adapter.calls == 2, "automatic check must not consume a model generation"
    assert state.generations_used == 2
    assert controller.last_observation["auto_check"]["status"] == "pass"
    events = [e["type"] for e in controller.store.read_events("ep-verify-ok")]
    assert events.index("auto_check") < events.index("auto_check_completed")
    # No model request happens between apply and its verification.
    applied_at = events.index("patch_applied")
    completed_at = events.index("auto_check_completed")
    assert not any(t == "model_request" for t in events[applied_at + 1:completed_at])


def test_pending_verification_drains_before_any_model_action(repo_copy, state_dir):
    """A queued blind revert cannot act on an unverified fix: the drain
    checks first, so the revert is only ever evaluated against evidence."""
    find = '{"action":"find","query":"add subtracts calc","symbols":["add"]}'
    revert = json.dumps({"action": "patch", "operation": "revert_last",
                         "reason_code": "regression"})
    controller, _ = _mandatory(repo_copy, state_dir, [find, revert], "ep-blind-revert")
    controller.start()
    controller.step()  # find only; no patch yet
    state = controller.state
    tree = controller.workspace.tree_hash()
    state.pending_verification = PendingVerification(tree=tree, patch_sha="unverified")
    controller.store.save_state(state)
    calls_before = controller.adapter.calls
    assert controller.step() is True
    kinds = [e["type"] for e in controller.store.read_events("ep-blind-revert")]
    assert "auto_check" in kinds and "auto_check_completed" in kinds, \
        "pending verification must run before the model's action is executed"
    assert controller.state.pending_verification is None
    assert controller.state.auto_checks == 1
    assert controller.adapter.calls == calls_before + 1, "drain consumes no generation"


def test_gate_blocks_actions_while_pending_defense_in_depth(repo_copy, state_dir):
    """Even if a drain were skipped, dispatch refuses non-DONE actions while
    a verification is pending."""
    find = '{"action":"find","query":"add subtracts calc","symbols":["add"]}'
    patch = json.dumps({"action": "patch", "operation": "replace",
                        "edits": [{"file_id": 2, "old": CORRECT_OLD, "new": CORRECT_NEW}],
                        "expected_effect": "fix add"})
    controller, _ = _mandatory(repo_copy, state_dir, [find, patch], "ep-gate-pending")
    controller.start()
    controller.step()  # find
    state = controller.state
    state.pending_verification = PendingVerification(
        tree=controller.workspace.tree_hash(), patch_sha="unverified")
    controller.store.save_state(state)
    controller._drain_pending = lambda: True  # simulate the drain being bypassed
    assert controller.step() is True
    assert controller.last_observation["status"] == "rejected"
    assert controller.last_observation["code"] == "verification_pending"
    assert controller.state.pending_verification is not None, "pending must survive rejection"
    assert controller.state.patch_attempts == 0, "no unverified edit may be accepted"


def test_crash_before_auto_check_recovers_on_resume(repo_copy, state_dir):
    controller, _ = _mandatory(
        repo_copy, state_dir, _replace_script((CORRECT_OLD, CORRECT_NEW)), "ep-crash-verify")
    controller.start()
    controller.step()  # find
    real_auto = controller._run_automatic_check

    def boom():
        raise CrashError("simulated crash before automatic check")

    controller._run_automatic_check = boom
    with pytest.raises(CrashError):
        controller.step()  # patch applies, then crash
    assert controller.state.pending_verification is not None
    assert controller.state.generations_used == 2
    controller._run_automatic_check = real_auto
    revert = json.dumps({"action": "patch", "operation": "revert_last",
                         "reason_code": "regression"})
    done = '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'
    # Slots 0 and 1 were consumed before the crash; resume starts at slot 2.
    controller2, adapter2 = make_controller(
        repo_copy, state_dir, ["unused", "unused", revert, done], episode_id="ep-crash-verify",
        protocol_version="2", verification_policy="mandatory")
    final = controller2.resume("ep-crash-verify")
    events = controller2.store.read_events("ep-crash-verify")
    kinds = [e["type"] for e in events]
    assert kinds.index("auto_check") < kinds.index("patch_reverted"), \
        "resume must verify before accepting the revert"
    assert final.auto_checks == 2, "drain check + post-revert check"
    assert adapter2.calls == 2  # revert + done only; drain consumed no generation


def test_wrong_edit_produces_failure_evidence(repo_copy, state_dir):
    controller, adapter = _mandatory(
        repo_copy, state_dir, _replace_script((CORRECT_OLD, WRONG_NEW)), "ep-verify-fail")
    controller.start()
    controller.step()  # find
    assert controller.step() is True  # patch + automatic check
    state = controller.state
    assert state.auto_checks == 1
    assert state.current.status == "fail"
    assert state.current.failed, "wrong edit must yield real failure evidence"
    assert state.outcome == "running", "a failing check is evidence, not a stop"
    assert controller.last_observation["auto_check"]["status"] == "fail"
    assert adapter.calls == 2


def test_full_recovery_flow_within_budgets(repo_copy, state_dir):
    revert = json.dumps({"action": "patch", "operation": "revert_last",
                         "reason_code": "regression"})
    script = _replace_script((CORRECT_OLD, WRONG_NEW))[:2] + [revert] + [
        json.dumps({"action": "patch", "operation": "replace",
                    "edits": [{"file_id": 2, "old": CORRECT_OLD, "new": CORRECT_NEW}],
                    "expected_effect": "fix add"}),
        '{"action":"done","status":"solved","summary":"Fixed add()."}',
    ]
    controller, adapter = _mandatory(repo_copy, state_dir, script, "ep-verify-flow")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.test_invocations == 5, "baseline + 3 automatic + final must fit the cap exactly"
    assert state.auto_checks == 3
    assert state.generations_used == 5
    assert adapter.calls == 5
    assert state.pending_verification is None


def test_focused_pass_cannot_bypass_final(repo_copy, state_dir):
    controller, _ = _mandatory(
        repo_copy, state_dir, _replace_script((CORRECT_OLD, CORRECT_NEW)), "ep-verify-final")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.final is not None and state.final.scope == "final"
    assert state.final_validations == 1
    assert state.current.scope == "focused", "last model-visible evidence stays focused"
    assert state.final.status == "pass"


def test_no_budget_blocks_honestly_without_apply(repo_copy, state_dir):
    controller, adapter = _mandatory(
        repo_copy, state_dir, _replace_script((CORRECT_OLD, CORRECT_NEW)), "ep-verify-budget")
    controller.start()
    controller.step()  # find
    state = controller.state
    before = controller.workspace.tree_hash()
    state.test_invocations = state.budgets.max_test_invocations
    controller.store.save_state(state)
    assert controller.step() is False
    assert state.outcome == "blocked"
    assert "validation budget" in (state.stop_reason or "")
    assert controller.workspace.tree_hash() == before, "nothing may be applied without verification room"
    assert state.generations_used == 2, "the refused decision still consumed its slot"


def test_policy_off_leaves_legacy_behavior(repo_copy, state_dir):
    controller, adapter = make_controller(
        repo_copy, state_dir, _replace_script((CORRECT_OLD, CORRECT_NEW)), "ep-verify-off",
        protocol_version="2")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.auto_checks == 0
    assert state.pending_verification is None
    assert state.test_invocations == 3, "baseline + model check + final only"
