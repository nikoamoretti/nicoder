"""DONE: solved is validated, never asserted; blocked always hands off."""
from local_coder.inference.fake import demo_script
from local_coder.skills import done as done_skill

from .conftest import make_controller


def test_done_solved_before_verification_is_rejected(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [
        '{"action":"done","status":"solved","summary":"trust me"}',
        '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'])
    controller.start()
    assert controller.step() is True  # rejected, still running
    assert controller.last_observation["status"] == "not_solved"
    assert controller.state.outcome == "running"
    assert controller.step() is False
    assert controller.state.outcome == "blocked"


def test_wrong_patch_cannot_validate_solved(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:wrong"))
    state = controller.run()
    assert state.outcome == "blocked"
    assert state.current is not None and state.current.status == "fail"
    assert state.final is not None and state.final.status == "fail"
    handoff = (controller.store.episode_dir(state.episode_id) / "handoff.md").read_text()
    for section in ("## Goal", "## Relevant files", "## Attempted changes",
                    "## Latest failure", "## Why stopping"):
        assert section in handoff


def test_stale_evidence_rejected_by_validator(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:success"))
    state = controller.run()
    assert state.outcome == "solved"
    tampered = controller.workspace.root / "src/calc.py"
    tampered.write_text(tampered.read_text() + "\n# post-hoc edit\n")
    ok, reasons = done_skill.validate_solved(
        state, controller.workspace, controller.original_manifest, ["src/**/*.py"])
    assert ok is False
    assert any("stale" in reason for reason in reasons)


def test_prohibited_edits_rejected_by_validator(repo_copy, state_dir):
    from local_coder.state import CheckEvidence

    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    ws = controller.workspace
    (ws.root / "tests" / "test_calc.py").write_text(
        (ws.root / "tests" / "test_calc.py").read_text() + "\n")
    tree = ws.tree_hash()
    passing = CheckEvidence(scope="final", argv=["x"], exit_code=0, status="pass",
                            passed=list(controller.state.required_tests),
                            collected=list(controller.state.required_tests),
                            log_artifact="a", tree_hash=tree)
    controller.state.baseline = passing
    controller.state.current = passing
    from local_coder.state import PatchRecord

    controller.state.last_patch = PatchRecord(
        sha="abc", summary="x", files=["tests/test_calc.py"],
        base_tree="b", result_tree=tree, reverse_artifact="r")
    ok, reasons = done_skill.validate_solved(
        controller.state, ws, controller.original_manifest, ["src/**/*.py"])
    assert ok is False
    assert any("prohibited" in reason for reason in reasons)
