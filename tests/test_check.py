"""CHECK: real pytest evidence, identity comparison, no false success."""
import sys

from local_coder.execution.trusted_local import TrustedLocalBackend
from local_coder.skills import check as check_skill
from local_coder.skills.check import compare_to_baseline, evaluate

from .conftest import make_controller


def test_baseline_records_real_failing_ids(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    base = controller.state.baseline
    assert base is not None and base.status == "fail"
    assert len(base.failed) == 2 and len(base.passed) == 1
    assert any("test_add_positive" in t for t in base.failed)
    assert any("test_add_negative" in t for t in base.failed)
    assert any(t.endswith("test_mul") for t in base.passed)
    assert len(controller.state.required_tests) == 3
    assert controller.state.test_invocations == 1
    assert base.tree_hash == controller.workspace.tree_hash()


def test_zero_tests_is_incomplete_not_pass(tmp_path):
    repo = tmp_path / "empty"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    backend = TrustedLocalBackend()
    import tempfile
    from pathlib import Path

    from local_coder.persistence import EpisodeStore
    from local_coder.workspace import CandidateWorkspace

    store = EpisodeStore(tmp_path / "ep")
    store.create("e1")
    ws = CandidateWorkspace.create(repo, store.candidate_dir("e1"))
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "r.xml"
        argv = [sys.executable, "-m", "pytest", "tests/", "-q", "--junitxml", str(report)]
        result = backend.run(argv, ws.root, 60, 65536)
        parsed = None
        if report.exists():
            from local_coder.execution.pytest_parser import parse_junit

            parsed = parse_junit(report)
    evidence = evaluate("focused", argv, result.exit_code, result.timed_out, 0.1,
                        result.log_text, False, parsed, None, "tree",
                        "artifacts/x.log", None, [], set(), set())
    assert evidence.status == "incomplete"
    assert "no tests collected" in (evidence.incomplete_reason or "")


def test_missing_required_test_is_incomplete(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    required = list(controller.state.required_tests)
    assert len(required) == 3
    # Keep the suite non-empty but drop two required tests: missing, not zero.
    target = controller.workspace.root / "tests" / "test_calc.py"
    target.write_text("from src.calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    evidence = check_skill.execute_scope(
        controller.state, controller.workspace, TrustedLocalBackend(), "focused",
        controller.config.commands["focused"], 60, 65536, "check-x.log",
        controller.store.write_artifact_text, controller.store.write_artifact_bytes)
    assert evidence.status == "incomplete"
    assert "no longer collected" in (evidence.incomplete_reason or "")


def test_false_pass_stdout_is_not_evidence(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    test_file = controller.workspace.root / "tests" / "test_calc.py"
    test_file.write_text(test_file.read_text() + '\n\ndef test_loud():\n    print("PASS")\n    assert False\n')
    evidence = check_skill.execute_scope(
        controller.state, controller.workspace, TrustedLocalBackend(), "focused",
        controller.config.commands["focused"], 60, 65536, "check-x.log",
        controller.store.write_artifact_text, controller.store.write_artifact_bytes)
    assert evidence.status == "fail"
    assert any("test_loud" in t for t in evidence.failed)


def test_newly_skipped_required_test_is_incomplete(tmp_path, state_dir):
    repo = tmp_path / "skips"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_two.py").write_text(
        "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n")
    (repo / "conftest.py").write_text("")
    (repo / "local-coder.toml").write_text("")
    controller, _ = make_controller(repo, state_dir, [])
    controller.start()
    assert controller.state.baseline.status == "pass"
    target = controller.workspace.root / "tests" / "test_two.py"
    target.write_text("import pytest\ndef test_one():\n    assert True\n\n"
                      "@pytest.mark.skip(reason='newly skipped')\ndef test_two():\n    assert True\n")
    evidence = check_skill.execute_scope(
        controller.state, controller.workspace, TrustedLocalBackend(), "focused",
        controller.config.commands["focused"], 60, 65536, "check-x.log",
        controller.store.write_artifact_text, controller.store.write_artifact_bytes)
    assert evidence.status == "incomplete"
    assert "newly skipped" in (evidence.incomplete_reason or "")


def test_corrupt_report_is_incomplete():
    evidence = evaluate("focused", ["pytest"], 0, False, 0.1, "output", False,
                        None, "not xml at all", "tree", "artifacts/x.log",
                        None, [], set(), set())
    assert evidence.status == "incomplete"
    assert "corrupt" in (evidence.incomplete_reason or "")


def test_identity_comparison_catches_hidden_regression():
    from local_coder.state import CheckEvidence

    def ev(passed, failed):
        return CheckEvidence(scope="focused", argv=["x"], exit_code=1, status="fail",
                             passed=passed, failed=failed, collected=sorted(set(passed + failed)),
                             log_artifact="a", tree_hash="t")

    base = ev(["a", "b", "c"], ["d", "e"])
    current = ev(["a", "d"], ["b", "e"])
    comparison = compare_to_baseline(base, current)
    assert comparison["fixed"] == ["d"]
    assert comparison["new_failures"] == ["b"]
    assert comparison["regression"] is True
    assert comparison["regressed_tests"] == ["b"]
