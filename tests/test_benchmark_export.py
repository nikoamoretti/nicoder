"""Prompt 4: frozen suite integrity, validation, reports, trace export."""
import csv
import json
import shutil

from local_coder.benchmark import (
    aggregate,
    failure_category,
    gold_hidden_from_repo,
    load_manifest,
    run_suite,
    sanitize,
    validate_suite,
    verify_frozen,
    write_reports,
)
from local_coder.export import export_episode, export_turn, write_jsonl
from local_coder.inference.fake import ScriptedFakeAdapter, demo_script

from .conftest import make_controller

SUITE = "pilot/suite"


def _manifest(tmp_path=None):
    from pathlib import Path

    return load_manifest(Path(SUITE))


def test_manifest_partitions_and_freezes(tmp_path):
    from pathlib import Path

    manifest = _manifest()
    assert len(manifest["entries"]) == 20
    assert len(manifest["splits"]["dev"]) == 12
    assert len(manifest["splits"]["held-out"]) == 8
    assert manifest["frozen_note"].startswith("Do not modify held-out")
    for entry in manifest["entries"]:
        assert verify_frozen(Path(SUITE), entry) == [], entry["id"]
        assert gold_hidden_from_repo(Path(SUITE), entry) == [], entry["id"]


def test_freeze_detects_tampering(tmp_path):
    from pathlib import Path

    manifest = _manifest()
    entry = dict(manifest["entries"][0])
    assert verify_frozen(Path(SUITE), entry) == []
    tampered = dict(entry)
    tampered["hashes"] = dict(entry["hashes"], repo_tree="0" * 32)
    assert verify_frozen(Path(SUITE), tampered) == ["repo_tree"]


def test_suite_validation_passes():
    import sys
    from pathlib import Path

    result = validate_suite(Path(SUITE), sys.executable)
    assert result["valid"], result["invalid"]
    assert len(result["tasks"]) == 20


def test_failure_categories():
    assert failure_category("solved", "x") == "solved"
    assert failure_category("setup_failed", "x") == "setup-failed"
    assert failure_category("blocked", "decision budget exhausted (8 generations") == "blocked-budget"
    assert failure_category("blocked", "repeated invalid model output (x)") == "blocked-invalid-output"
    assert failure_category("blocked", "consecutive no-progress actions") == "blocked-no-progress"
    assert failure_category("blocked", "final validation failed") == "blocked-validation-failed"
    assert failure_category("blocked", "elapsed time budget exceeded") == "blocked-elapsed"
    assert failure_category("blocked", "something else") == "blocked-other"


def test_aggregate_denominators_explicit():
    tasks = [
        {"id": "a", "status": "solved", "category": "solved", "generations": 4,
         "regression": False, "recovery": False, "handoff_ok": None,
         "recall_at_1": 1.0, "recall_at_3": 1.0},
        {"id": "b", "status": "blocked", "category": "blocked-budget", "generations": 8,
         "regression": True, "recovery": True, "handoff_ok": True,
         "recall_at_1": 0.0, "recall_at_3": 0.5},
        {"id": "c", "status": "setup-failed", "category": "setup-failed"},
        {"id": "d", "status": "scheduled"},
    ]
    agg = aggregate({"tasks": tasks})
    assert agg["scheduled"] == 4 and agg["attempted"] == 3 and agg["completed"] == 2
    assert agg["solved"] == 1
    assert agg["solve_rate_completed"] == 0.5
    assert agg["solve_rate_attempted"] == round(1 / 3, 3)
    assert agg["by_status"] == {"solved": 1, "blocked": 1, "setup-failed": 1, "scheduled": 1}
    assert agg["with_regression"] == 1 and agg["with_recovery"] == 1
    assert agg["handoff_ok_blocked"] == 1 and agg["blocked_total"] == 1
    assert agg["mean_recall_at_1"] == 0.5


def test_reports_written_and_consistent(tmp_path):
    tasks = [{"id": "a", "family": "f", "split": "dev", "origin": "generated",
              "status": "solved", "category": "solved", "generations": 4,
              "patch_attempts": 1, "model_checks": 1, "test_invocations": 3,
              "prompt_tokens_exact": 0, "completion_tokens_exact": 0, "elapsed_s": 1.0,
              "protocol_errors": 0, "skill_accepted": 3, "skill_rejected": 0,
              "patch_applied": True, "regression": False, "recovery": False,
              "handoff_ok": None, "recall_at_1": 1.0, "recall_at_3": 1.0}]
    report = {"report_version": 1, "run_id": "r1", "suite": "pilot-20", "profile": "fake",
              "started": "t", "finished": "t", "splits": ["dev"], "tasks": tasks}
    paths = write_reports(report, tmp_path / "rep", "/Users/someone")
    loaded = json.loads(paths["json"].read_text())
    assert loaded["aggregate"]["solved"] == 1
    rows = list(csv.DictReader(paths["csv"].open()))
    assert len(rows) == 1 and rows[0]["id"] == "a" and rows[0]["status"] == "solved"
    md = paths["md"].read_text()
    assert "| a | dev | solved |" in md and "nothing dropped silently" in md


def test_sanitize_strips_home():
    assert sanitize({"p": "/Users/someone/x", "n": 1}, "/Users/someone") == {"p": "~/x", "n": 1}


def test_benchmark_smoke_two_tasks_fake(tmp_path, monkeypatch):
    import os
    import sys
    from pathlib import Path

    from local_coder.execution.trusted_local import TrustedLocalBackend
    from local_coder.persistence import EpisodeStore

    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    state_dir = tmp_path / "episodes"
    report = run_suite(
        Path(SUITE), "fake:success",
        lambda entry: ScriptedFakeAdapter(demo_script("fake:success"), "fake:success"),
        TrustedLocalBackend, state_dir, "smoke-run",
        splits=("dev",), max_tickets=2, report_dir=tmp_path / "rep")
    assert [t["id"] for t in report["tasks"]] == ["fam-demo-calc", "fam-localization"]
    assert report["tasks"][0]["status"] == "solved"
    assert report["attempted"] == 2
    assert (tmp_path / "rep" / "report.json").exists()
    assert (tmp_path / "rep" / "report.csv").exists()
    assert (tmp_path / "rep" / "report.md").exists()
    store = EpisodeStore(state_dir)
    assert store.load_state("smoke-run-fam-demo-calc").task_id == "fam-demo-calc"


def test_export_marks_legacy_incomplete_and_excludes_held_out(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:success"),
                                    episode_id="ep-export")
    controller.run()
    store = controller.store
    records = export_episode(store, "ep-export", split="dev")
    assert len(records) == 4
    assert all(r["complete"] for r in records)
    assert all(r["training_eligible"] for r in records)
    assert all(r["reviewed_corrections"] == [] for r in records)
    assert all(r["export_version"] == "1" for r in records)
    solved_turns = [r for r in records if r.get("verified_transition")]
    assert solved_turns, "expected verified transitions in a solved episode"
    # Held-out records never train by default.
    held = export_episode(store, "ep-export", split="held-out")
    assert held and all(not r["training_eligible"] for r in held)
    flagged = export_episode(store, "ep-export", split="held-out", include_held_out=True)
    assert any(r["training_eligible"] for r in flagged)
    # Simulate a legacy episode: no turn bundles at all.
    shutil.rmtree(store.episode_dir("ep-export") / "turns")
    legacy = export_episode(store, "ep-export", split="dev")
    assert len(legacy) == 4
    assert all(not r["complete"] for r in legacy)
    assert all(not r["training_eligible"] for r in legacy)
    assert all(r["missing"] for r in legacy)


def test_export_preserves_rejected_actions_as_negatives(repo_copy, state_dir):
    import json

    find = demo_script("fake:success")[0]
    bad_patch = json.dumps({"action": "patch", "operation": "apply",
                            "diff": "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1 +1 @@\n-x\n+y\n",
                            "expected_effect": "bad hunk"})
    blocked = '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'
    controller, _ = make_controller(repo_copy, state_dir, [find, bad_patch, blocked],
                                    episode_id="ep-neg")
    controller.run()
    store = controller.store
    records = export_episode(store, "ep-neg", split="dev")
    rejected = [r for r in records
                if (r.get("observation") or {}).get("status") == "rejected"]
    assert rejected, "rejected patch turn must survive inside the export"
    assert all(not r["verified_transition"] for r in rejected)
    assert all(r["complete"] for r in rejected)


def test_export_turn_roundtrip_with_manifest(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:success"),
                                    episode_id="ep-round")
    controller.run()
    store = controller.store
    record = export_turn(store, "ep-round", 2, split="dev")
    assert record["complete"] and record["training_eligible"]
    assert record["briefing"] and "TASK" in record["briefing"]
    assert record["state_pre"]["generations_used"] == 1
    assert record["state_post"]["generations_used"] == 2
    assert record["action"] and record["action"]["action"] == "patch"
    assert record["manifest"]["files"]
    dest = store.episode_dir("ep-round") / "traces.jsonl"

    write_jsonl([record], dest)
    assert dest.exists()


def test_export_traces_cli_wiring(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from local_coder.inference.fake import demo_script

    from .conftest import FIXTURE_DIR, make_controller

    repo = tmp_path / "repo"
    import shutil

    shutil.copytree(FIXTURE_DIR / "repo", repo)
    controller, _ = make_controller(repo, tmp_path / "ep", demo_script("fake:success"),
                                    episode_id="ep-cli-exp")
    controller.run()
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
    out = tmp_path / "traces.jsonl"
    run = subprocess.run(
        [sys.executable, "-m", "local_coder.cli", "--state-dir", str(tmp_path / "ep"),
         "export-traces", "--output", str(out), "--episodes", "ep-cli-exp"],
        capture_output=True, text=True, env=env, timeout=120)
    assert run.returncode == 0, run.stderr
    import json as _json

    records = [_json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(records) == 4
    assert all(r["export_version"] == "1" for r in records)


def test_committed_pilot_report_is_self_consistent():
    import csv
    import json as _json
    from pathlib import Path

    report_path = Path(__file__).parent.parent / "pilot" / "reports" / "pilot-01" / "report.json"
    if not report_path.exists():
        import pytest as _pytest

        _pytest.skip("pilot report not present in this checkout")
    report = _json.loads(report_path.read_text())
    from local_coder.benchmark import aggregate

    recomputed = aggregate(report)
    assert recomputed == report["aggregate"]
    rows = list(csv.DictReader(open(str(report_path.parent / "report.csv"))))
    assert len(rows) == len(report["tasks"]) == 20
    assert {r["id"] for r in rows} == {t["id"] for t in report["tasks"]}
    md = (report_path.parent / "report.md").read_text()
    assert "pilot-01" in md
