"""Corrections A.1/A.2: fixed means explicitly passing; exit codes rule.

A parseable report is necessary but never sufficient. Skipped, missing, or
unexecuted tests are never fixes, and a broken runner (exit 2/3/4/5/unknown
or a contradicting report) is never a pass — even with passing tests inside.
"""
from local_coder.briefing import compact_observation, progress_text
from local_coder.execution.pytest_parser import ParsedSuite, classify_exit_code
from local_coder.skills import done as done_skill
from local_coder.skills.check import compare_to_baseline, evaluate
from local_coder.state import CheckEvidence


def _ev(status, passed=(), failed=(), errors=(), skipped=(), collected=None,
        incomplete=None, exit_code=1):
    all_ids = sorted(set(passed) | set(failed) | set(errors) | set(skipped))
    return CheckEvidence(
        scope="focused", argv=["pytest"], exit_code=exit_code, status=status,
        passed=sorted(passed), failed=sorted(failed), errors=sorted(errors),
        skipped=sorted(skipped),
        collected=sorted(collected) if collected is not None else all_ids,
        log_artifact="a", tree_hash="t", incomplete_reason=incomplete)


def _parsed(passed=(), failed=(), errors=(), skipped=()):
    suite = ParsedSuite()
    suite.passed = sorted(passed)
    suite.failed = sorted(failed)
    suite.errors = sorted(errors)
    suite.skipped = sorted(skipped)
    suite.collected = sorted(set(passed) | set(failed) | set(errors) | set(skipped))
    return suite


def _run(parsed, exit_code, required=(), baseline_skipped=frozenset()):
    return evaluate("focused", ["pytest"], exit_code, False, 0.1, "log", False,
                    parsed, None, "t", "a", None, list(required),
                    set(baseline_skipped), set())


def test_focused_run_missing_baseline_failure_claims_no_fix():
    baseline = _ev("fail", passed=["t1"], failed=["t2", "t3"])
    current = _ev("incomplete", passed=["t1", "t2"], collected=["t1", "t2"],
                  incomplete="required tests no longer collected: ['t3']", exit_code=1)
    comparison = compare_to_baseline(baseline, current)
    assert comparison["comparable"] is False
    assert comparison["fixed"] == []
    assert comparison["missing"] == ["t3"]
    text = progress_text(baseline, current, False)
    assert "no fix claimed" in text and "missing: ['t3']" in text


def test_previously_failing_but_skipped_is_not_fixed():
    baseline = _ev("fail", passed=["t1"], failed=["t2"])
    current = _ev("incomplete", passed=["t1"], skipped=["t2"], collected=["t1", "t2"],
                  incomplete="required tests newly skipped: ['t2']", exit_code=0)
    comparison = compare_to_baseline(baseline, current)
    assert comparison["fixed"] == []
    assert comparison["skipped_now"] == ["t2"]
    text = progress_text(baseline, current, False)
    assert "no fix claimed" in text and "skipped now: ['t2']" in text


def test_genuine_fix_now_passing():
    baseline = _ev("fail", passed=["t1"], failed=["t2"])
    current = _ev("pass", passed=["t1", "t2"], collected=["t1", "t2"], exit_code=0)
    comparison = compare_to_baseline(baseline, current)
    assert comparison["comparable"] is True
    assert comparison["fixed"] == ["t2"]
    assert comparison["still_failing"] == []
    text = progress_text(baseline, current, False)
    assert "fixed (explicitly passing now): ['t2']" in text


def test_incomplete_partial_report_claims_no_fix():
    baseline = _ev("fail", failed=["t1", "t2"])
    current = _ev("incomplete", failed=["t1"], collected=["t1"],
                  incomplete="timeout", exit_code=None)
    comparison = compare_to_baseline(baseline, current)
    assert comparison["comparable"] is False
    assert comparison["fixed"] == []
    assert comparison["new_failures"] == []
    assert comparison["regression"] is False


def test_incomplete_baseline_is_incomparable():
    baseline = _ev("incomplete", incomplete="timeout", exit_code=None)
    current = _ev("pass", passed=["t1"], collected=["t1"], exit_code=0)
    comparison = compare_to_baseline(baseline, current)
    assert comparison["comparable"] is False
    assert comparison["fixed"] == []


def test_exit_codes_classified():
    assert classify_exit_code(0) == "ok"
    assert classify_exit_code(1) == "tests_failed"
    assert classify_exit_code(2) == "interrupted"
    assert classify_exit_code(3) == "internal_error"
    assert classify_exit_code(4) == "usage_error"
    assert classify_exit_code(5) == "no_tests"
    assert classify_exit_code(99) == "unknown"
    assert classify_exit_code(None) == "not_exited"


def test_internal_error_with_passing_report_is_incomplete():
    evidence = _run(_parsed(passed=["t1", "t2"]), exit_code=3)
    assert evidence.status == "incomplete"
    assert "internal error" in (evidence.incomplete_reason or "")
    assert evidence.passed == ["t1", "t2"]


def test_interrupted_usage_no_tests_unknown_are_incomplete():
    assert _run(_parsed(passed=["t1"]), exit_code=2).status == "incomplete"
    assert _run(_parsed(passed=["t1"]), exit_code=4).status == "incomplete"
    assert _run(_parsed(passed=["t1"]), exit_code=5).status == "incomplete"
    assert _run(_parsed(passed=["t1"]), exit_code=99).status == "incomplete"


def test_exit_zero_with_reported_failures_is_inconsistent():
    evidence = _run(_parsed(passed=["t1"], failed=["t2"]), exit_code=0)
    assert evidence.status == "incomplete"
    assert "contradicts" in (evidence.incomplete_reason or "")


def test_exit_one_with_clean_report_is_inconsistent():
    evidence = _run(_parsed(passed=["t1"]), exit_code=1)
    assert evidence.status == "incomplete"
    assert "no failures" in (evidence.incomplete_reason or "")


def test_allowed_failure_still_passes_on_completed_run():
    evidence = evaluate("focused", ["pytest"], 1, False, 0.1, "log", False,
                        _parsed(passed=["t1"], failed=["t2"]), None, "t", "a",
                        None, ["t1", "t2"], set(), {"t2"})
    assert evidence.status == "pass"


def test_internal_error_evidence_rejected_by_done_validation(tmp_path, state_dir):
    import shutil

    from local_coder.state import PatchRecord

    from .conftest import FIXTURE_REPO, make_controller

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo)
    controller, _ = make_controller(repo, state_dir, [])
    controller.start()
    ws = controller.workspace
    broken = CheckEvidence(
        scope="final", argv=["x"], exit_code=3, status="incomplete",
        passed=list(controller.state.required_tests),
        collected=list(controller.state.required_tests),
        log_artifact="a", tree_hash=ws.tree_hash(),
        incomplete_reason="pytest internal error (exit 3); report content is not evidence")
    controller.state.baseline = _ev("fail", failed=["t"])
    controller.state.current = broken
    controller.state.final = broken
    controller.state.last_patch = PatchRecord(
        sha="abc", summary="x", files=[], base_tree="b",
        result_tree=ws.tree_hash(), reverse_artifact="r")
    ok, reasons = done_skill.validate_solved(
        controller.state, ws, controller.original_manifest, ["src/**/*.py"])
    assert ok is False
    assert any("incomplete" in reason for reason in reasons)


def test_compact_observation_never_cuts_json_mid_field():
    import json

    observation = {
        "status": "ok",
        "hits": [{"path": f"src/mod{i}.py", "reasons": [f"reason {i} " * 20]} for i in range(12)],
        "failed": [f"test_{i}" for i in range(30)],
    }
    text = compact_observation(observation, budget_chars=800)
    assert len(text) <= 800
    parsed = json.loads(text)  # must parse: no mid-field cut
    assert parsed["status"] == "ok"
    assert "_dropped_keys" in parsed or "_shortened_lists" in parsed
