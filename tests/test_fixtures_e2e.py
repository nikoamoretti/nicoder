"""Prompt 2 fixture evidence: localization, multi-hunk, recovery."""
import json
import shutil

from local_coder.workspace import parse_unified_diff

from .conftest import fixture_paths, make_controller


def _copy(paths, tmp_path, name):
    repo = tmp_path / name
    shutil.copytree(paths["repo"], repo)
    return repo


def _patch_action(diff_text, effect):
    return json.dumps({"action": "patch", "operation": "apply", "diff": diff_text,
                       "expected_effect": effect})


def test_multi_hunk_gold_has_two_hunks_and_solves(tmp_path, state_dir):
    paths = fixture_paths("multi_hunk")
    assert len(parse_unified_diff(paths["gold"].read_text())[0].hunks) == 2
    repo = _copy(paths, tmp_path, "mh")
    gold = paths["gold"].read_text()
    script = [
        '{"action":"find","query":"order totals quantity discount",'
        '"symbols":["line_total","grand_total"]}',
        _patch_action(gold, "fix quantity and discount"),
        '{"action":"check","scope":"focused"}',
        '{"action":"done","status":"solved","summary":"Fixed both pricing bugs."}',
    ]
    controller, _ = make_controller(repo, state_dir, script, ticket_file=paths["ticket"],
                                    episode_id="ep-multihunk")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.last_patch is not None and state.last_patch.files == ["src/pricing.py"]
    assert len(state.required_tests) == 3


def test_localization_finds_and_solves(tmp_path, state_dir):
    paths = fixture_paths("localization")
    repo = _copy(paths, tmp_path, "loc")
    gold = paths["gold"].read_text()
    script = [
        '{"action":"find","query":"refresh expired session redirect_uri",'
        '"symbols":["refresh_session"]}',
        _patch_action(gold, "preserve redirect_uri on refresh"),
        '{"action":"check","scope":"focused"}',
        '{"action":"done","status":"solved","summary":"Preserved redirect target."}',
    ]
    controller, _ = make_controller(repo, state_dir, script, ticket_file=paths["ticket"],
                                    episode_id="ep-localization")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert [f.path for f in state.focus if f.path == "src/session.py"], "gold file never in focus"


def test_recovery_wrong_patch_revert_correct(tmp_path, state_dir):
    paths = fixture_paths("recovery")
    repo = _copy(paths, tmp_path, "rec")
    gold = paths["gold"].read_text()
    wrong = (paths["repo"].parent / "wrong.diff").read_text()
    script = [
        '{"action":"find","query":"counter increment reset","symbols":["Counter","increment"]}',
        _patch_action(wrong, "fix increment"),
        '{"action":"check","scope":"focused"}',
        '{"action":"patch","operation":"revert_last","reason_code":"regression"}',
        '{"action":"check","scope":"focused"}',
        _patch_action(gold, "fix increment only"),
        '{"action":"check","scope":"focused"}',
        '{"action":"done","status":"solved","summary":"Fixed increment without breaking reset."}',
    ]
    controller, _ = make_controller(repo, state_dir, script, ticket_file=paths["ticket"],
                                    episode_id="ep-recovery")
    controller.start()
    assert controller.step() is True  # FIND
    assert controller.step() is True  # PATCH wrong
    assert controller.step() is True  # CHECK: regression, not masked progress
    obs = controller.last_observation
    assert obs["status"] == "fail"
    assert obs["regression"] is True
    assert any("test_reset" in t for t in obs["new_vs_baseline"]), obs
    assert len(obs["fixed_vs_baseline"]) == 2
    while (controller.state.outcome == "running"
           and controller.state.generations_used < controller.state.budgets.max_generations
           and controller.step()):
        pass
    state = controller.state
    assert state.outcome == "solved", state.stop_reason
    assert state.generations_used == 8
    assert state.patch_attempts == 3
    assert state.model_checks == 3
    assert state.test_invocations == 5
    events = controller.store.read_events("ep-recovery")
    regressions = [e for e in events if e["type"] == "check_completed"
                   and e["data"].get("regression") == "True"]
    assert regressions, "expected a recorded regression before recovery"
    assert any(e["type"] == "patch_reverted" for e in events)
