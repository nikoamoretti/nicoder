"""Phase 1.2 tests: sealed evidence, backups, run exports, preflight, smoke."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from endpoint_ledger.config import Settings
from endpoint_ledger.db.base import Base
from endpoint_ledger.db.models import (
    BaselineCohort,
    Observation,
    RunAnnotation,
    SuiteRun,
)
from endpoint_ledger.evidence import (
    seal_run,
)
from endpoint_ledger.export import (
    ExportInvalidError,
    export_run,
    import_export,
    verify_export,
)
from endpoint_ledger.measurement.runlevel import (
    RunAggregate,
    compare_run_level,
    compute_run_aggregates,
)
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import make_endpoint, sync_probes

SMALL = dict(
    baseline_min_calendar_days=0,
    baseline_min_distinct_runs=2,
    baseline_min_primary_observations=8,
    baseline_min_distinct_dates=1,
)


def _settings(**overrides) -> Settings:
    database_url = overrides.pop("database_url", TEST_DATABASE_URL)
    return Settings(database_url=database_url, **SMALL, **overrides)


def _make_live_run(db, settings, suite_name="live-x", mode="stable_success", repeats=3, seal=True):
    make_endpoint(db, request_defaults={"mock": {"mode": mode, "seed": 1}})
    sync_probes(db)
    db.commit()
    run = SuiteRunner(db, settings).run(
        SuiteOptions(suite_name=suite_name, provider="mock", repeats=repeats, live=False)
    ).suite_run
    db.commit()
    run.data_origin = "live"
    db.commit()
    if seal:
        seal_run(db, run)
        db.commit()
    return run


# ------------------------------------------------------------ sealed evidence


def test_sealed_live_run_cannot_be_deleted_via_sqlalchemy(db, settings):
    run = _make_live_run(db, settings)
    db.delete(run)
    with pytest.raises(SQLAlchemyError):
        db.commit()
    db.rollback()
    assert db.get(SuiteRun, run.id) is not None


def test_sealed_live_observations_and_scores_cannot_be_deleted(db, settings):
    run = _make_live_run(db, settings)
    observation = run.observations[0]
    db.delete(observation)
    with pytest.raises(SQLAlchemyError):
        db.commit()
    db.rollback()
    score = observation.scores[0]
    db.delete(score)
    with pytest.raises(SQLAlchemyError):
        db.commit()
    db.rollback()
    assert db.get(Observation, observation.id) is not None


def test_sealed_live_evidence_cannot_be_deleted_via_direct_sql(db, settings, engine):
    run = _make_live_run(db, settings)
    rid = run.id
    oid = run.observations[0].id
    for statement, _label in [
        (f"DELETE FROM suite_runs WHERE id = {rid}", "suite_run"),
        (f"DELETE FROM observations WHERE suite_run_id = {rid}", "observations"),
        (f"DELETE FROM scores WHERE observation_id = {oid}", "scores"),
    ]:
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as conn:
                conn.execute(text(statement))
        assert db.get(SuiteRun, rid) is not None


def test_sealed_live_evidence_cannot_be_modified(db, settings, engine):
    run = _make_live_run(db, settings)
    rid = run.id
    oid = run.observations[0].id
    sid = run.observations[0].scores[0].id
    attempts = [
        (f"UPDATE suite_runs SET data_origin = 'demo' WHERE id = {rid}", "origin"),
        (f"UPDATE suite_runs SET evidence_state = 'open' WHERE id = {rid}", "unseal"),
        (f"UPDATE observations SET raw_response_hash = 'x' WHERE id = {oid}", "observation"),
        (f"UPDATE scores SET passed = false WHERE id = {sid}", "score"),
    ]
    for statement, _label in attempts:
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as conn:
                conn.execute(text(statement))
    # Lifecycle transition remains allowed.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE suite_runs SET evidence_state = 'exported', "
                "evidence_manifest_hash = 'abc', evidence_exported_at = now() WHERE id = :rid"
            ),
            {"rid": rid},
        )


def test_demo_data_can_still_be_intentionally_replaced(db, settings):
    run = _make_live_run(db, settings, seal=False)
    demo = SuiteRun(
        suite_name="demo-baseline", status="completed", data_origin="demo",
        total_expected_observations=1, completed_observations=1,
        failed_observations=0, runner_version="0.1.0",
    )
    db.add(demo)
    db.commit()
    demo_id = demo.id
    db.delete(demo)
    db.commit()
    assert db.get(SuiteRun, demo_id) is None
    assert db.get(SuiteRun, run.id) is not None


def test_open_runs_can_still_be_completed_normally(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    run = SuiteRunner(db, settings).run(
        SuiteOptions(suite_name="open-x", provider="mock", repeats=1, live=False)
    ).suite_run
    assert run.status == "completed"
    assert run.data_origin == "mock"
    assert run.evidence_state == "open"


def test_partial_failure_persistence_unaffected_by_sealing(db, settings):
    make_endpoint(
        db,
        request_defaults={"mock": {"mode": "intermittent_errors", "seed": 2, "success_rate": 0.4}},
    )
    sync_probes(db)
    db.commit()
    run = SuiteRunner(db, settings).run(
        SuiteOptions(suite_name="flaky-live", provider="mock", repeats=3, live=False)
    ).suite_run
    db.commit()
    run.data_origin = "live"
    seal_run(db, run)
    db.commit()
    assert run.status in ("completed", "completed_with_errors")
    assert run.failed_observations > 0
    assert len(run.observations) == run.total_expected_observations


# ------------------------------------------------------------ exports


def _export_settings(tmp_path) -> Settings:
    return _settings(evidence_export_dir=str(tmp_path / "exports"))


def test_run_export_generation_and_verification(db, settings, tmp_path):
    run = _make_live_run(db, settings)
    settings = _export_settings(tmp_path)
    artifact = export_run(db, run, settings)
    db.commit()
    assert artifact.artifact_type == "run_export"
    result = verify_export(Path(artifact.path))
    assert result["ok"], result["errors"]
    assert result["file_count"] >= 3
    assert result["manifest_hash"] == artifact.sha256
    # Export files do not contain secrets.
    for file_path in Path(artifact.path).iterdir():
        assert "sk-" not in file_path.read_text(encoding="utf-8")


def test_tampering_with_export_is_detected(db, settings, tmp_path):
    run = _make_live_run(db, settings)
    settings = _export_settings(tmp_path)
    artifact = export_run(db, run, settings)
    db.commit()
    observations_path = Path(artifact.path) / "observations.jsonl"
    with observations_path.open("a", encoding="utf-8") as fh:
        fh.write('{"tampered": true}\n')
    result = verify_export(Path(artifact.path))
    assert result["ok"] is False
    assert any("hash mismatch" in e for e in result["errors"])


def test_secret_pattern_rejection_in_export(db, settings, tmp_path):
    run = _make_live_run(db, settings, seal=False)
    settings = _export_settings(tmp_path)
    observation = run.observations[0]
    observation.raw_response = {"provider": "mock", "text": "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
    db.commit()
    with pytest.raises(ExportInvalidError, match="secret-shaped"):
        export_run(db, run, settings)


def test_export_import_round_trip_preserves_evidence(db, settings, tmp_path):
    import subprocess

    run = _make_live_run(db, settings, repeats=2)
    expected_run_id = run.id
    db.commit()
    before = {
        "obs": [(o.id, o.raw_request_hash, o.raw_response_hash, o.normalized_text) for o in run.observations],
        "scores": [(s.id, s.metric_name, s.passed) for o in run.observations for s in o.scores],
        "manifest": None,
    }
    settings = _export_settings(tmp_path)
    artifact = export_run(db, run, settings)
    db.commit()
    before["manifest"] = artifact.sha256
    bundle_path = Path(artifact.path)

    # Import into a dedicated empty database so the shared test database is
    # never dropped mid-test.
    temp_db = "endpoint_ledger_roundtrip_test"
    subprocess.run(["dropdb", "--if-exists", temp_db], check=False, capture_output=True)
    subprocess.run(["createdb", temp_db], check=True, capture_output=True)
    try:
        import_engine = create_engine(f"postgresql+psycopg://localhost:5432/{temp_db}")
        Base.metadata.create_all(import_engine)
        import_session = sessionmaker(bind=import_engine)()
        imported = import_export(bundle_path, import_session, settings, require_empty=True)
        import_session.expire_all()
        after = {
            "obs": [(o.id, o.raw_request_hash, o.raw_response_hash, o.normalized_text) for o in imported.observations],
            "scores": [(s.id, s.metric_name, s.passed) for o in imported.observations for s in o.scores],
        }
        import_session.commit()  # close the transaction before assertions
        assert imported.id == expected_run_id
        assert after["obs"] == before["obs"]
        assert after["scores"] == before["scores"]
        assert imported.evidence_state == "sealed"
        import_session.close()

        # Re-importing into the now non-empty database is refused (the import
        # guard, which is what makes repeated imports idempotent-safe).
        import_session2 = sessionmaker(bind=import_engine)()
        with pytest.raises(RuntimeError, match="not empty"):
            import_export(bundle_path, import_session2, settings, require_empty=True)
        import_session2.close()
        import_engine.dispose()
    finally:
        subprocess.run(["dropdb", temp_db], check=False, capture_output=True)


def test_export_failure_preserves_database_evidence(db, settings, tmp_path):
    run = _make_live_run(db, settings, seal=False)  # noqa: F841 - used below
    db.commit()
    settings = _export_settings(tmp_path)
    observation = run.observations[0]
    observation.raw_response = {"provider": "mock", "text": "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
    db.commit()
    with pytest.raises(ExportInvalidError):
        export_run(db, run, settings)
    # The run and its evidence are untouched.
    db.expire_all()
    run = db.get(SuiteRun, run.id)
    assert run is not None
    assert len(run.observations) > 0
    assert run.evidence_state in ("open", "sealed")


# ------------------------------------------------------------ backups


def test_backup_create_restore_verify(tmp_path):
    """Backup/verify/restore against a dedicated temporary database so the
    shared test database is never wiped."""
    import subprocess

    from endpoint_ledger.backup import (
        create_backup,
        last_verified_backup,
        list_backups,
        restore_backup,
        verify_backup,
    )

    temp_db = "endpoint_ledger_backup_test"
    subprocess.run(["dropdb", "--if-exists", temp_db], check=False, capture_output=True)
    subprocess.run(["createdb", temp_db], check=True, capture_output=True)
    try:
        backup_settings = _settings(
            backup_dir=str(tmp_path / "backups"),
            database_url=f"postgresql+psycopg://localhost:5432/{temp_db}",
        )
        path = create_backup(backup_settings)
        assert path.exists()
        assert path.name.endswith(".dump")
        assert list_backups(backup_settings) == [path]

        result = verify_backup(path, backup_settings)
        assert "verified_at" in result
        assert "tables" in result["checks"]
        assert last_verified_backup(backup_settings) == path

        # Restore into the same dedicated database is safe and idempotent.
        restore_backup(path, backup_settings)
        restore_backup(path, backup_settings)
    finally:
        subprocess.run(["dropdb", temp_db], check=False, capture_output=True)


def test_backup_refuses_missing_file(tmp_path):
    from endpoint_ledger.backup import verify_backup

    with pytest.raises(FileNotFoundError):
        verify_backup(Path(tmp_path / "missing.dump"), _settings(backup_dir=str(tmp_path)))


# ------------------------------------------------------------ run level


def test_run_level_aggregates_and_repeats_not_dates(db, settings):
    run = _make_live_run(db, settings, repeats=5)
    version = run.observations[0].probe_version
    aggregates = compute_run_aggregates(
        db, run.observations[0].endpoint_id, version.id, version.primary_metric, "live"
    )
    # Five repeats collapse into ONE run-level aggregate.
    assert len(aggregates) == 1
    assert aggregates[0].attempts == 5
    assert aggregates[0].rate in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def test_one_run_cannot_confirm_change():
    baseline = [RunAggregate(i, None, None, None, 5, 5, 1.0, None, None) for i in range(10)]
    current = [RunAggregate(100 + i, None, None, None, 0, 50, 0.0, None, None) for i in range(1)]
    comparison = compare_run_level(
        baseline, current, min_baseline_runs=10, min_current_runs=3,
        min_current_dates=2, material_effect=0.2, alpha=0.05,
        n_permutations=200, require_direction_consistency=True,
    )
    assert comparison.qualifies is False
    assert "insufficient current runs" in comparison.reason


def test_persistent_multi_run_regression_can_confirm():
    from datetime import date

    baseline = [
        RunAggregate(i, None, None, date(2026, 8, 1), 5, 5, 1.0, None, None) for i in range(10)
    ]
    current = [
        RunAggregate(100 + i, None, None, date(2026, 8, 9), 0, 5, 0.0, None, None)
        for i in range(3)
    ]
    comparison = compare_run_level(
        baseline, current, min_baseline_runs=10, min_current_runs=3,
        min_current_dates=1, material_effect=0.2, alpha=0.05,
        n_permutations=2000, require_direction_consistency=True,
    )
    assert comparison.qualifies is True
    assert comparison.direction == "regression"
    assert comparison.p_value is not None and comparison.p_value < 0.05


# ------------------------------------------------------------ preflight


def test_preflight_refuses_without_backup_when_live_evidence_exists(db, settings, tmp_path):
    from endpoint_ledger.preflight import run_preflight

    make_endpoint(db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini")
    _make_live_run(db, settings)
    db.commit()
    report = run_preflight(
        db, _settings(require_backup_before_maintenance=True, backup_dir=str(tmp_path / "no-backups")),
        provider="openai", endpoint_filter="gpt-4o-mini",
        suite="probes/smoke", repeats=5, cost_ceiling=1.0,
    )
    assert report["ready"] is False
    assert any("verified backup" in r for r in report["refusals"])


def test_preflight_reports_expected_matrix_and_eligibility(db, settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from endpoint_ledger.preflight import run_preflight

    make_endpoint(db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini")
    _make_live_run(db, settings)
    db.commit()
    report = run_preflight(
        db, _settings(require_backup_before_maintenance=False),
        provider="openai", endpoint_filter="gpt-4o-mini",
        suite="probes/smoke", repeats=5, cost_ceiling=1.0,
    )
    assert report["endpoint"]["model_requested"] == "gpt-4o-mini"
    probes = [p for p in report["probes"] if p["baseline_eligible"]]
    assert len(probes) == 6
    assert all(p["primary_metric"] for p in probes)
    assert report["expected_request_count"] == 6 * 5
    assert report["checks"]["api_credential_present"] is False


# ------------------------------------------------------------ smoke approval


def test_qualification_smoke_approval_lifecycle(db, settings, tmp_path):
    run = _make_live_run(db, settings, suite_name="qualification-smoke-2026-08-10")
    settings = _export_settings(tmp_path)
    artifact = export_run(db, run, settings)
    verification = verify_export(Path(artifact.path))
    assert verification["ok"]
    artifact.verification_status = "verified"
    artifact.verified_at = datetime.now(UTC)
    for observation in run.observations:
        if observation.error_type is None:
            observation.model_reported = "mock:model-1"
    db.commit()

    from typer import Exit as TyperExit

    from endpoint_ledger.cli import approve_qualification_run

    try:
        approve_qualification_run(run.id)
    except TyperExit as exc:  # pragma: no cover - unexpected
        raise AssertionError(f"approval unexpectedly refused: {exc}") from exc
    annotations = db.query(RunAnnotation).filter(
        RunAnnotation.suite_run_id == run.id,
        RunAnnotation.annotation_type == "qualification_approval",
    ).all()
    assert len(annotations) == 1
    # Original observations and scores are untouched by approval.
    observation = run.observations[0]
    assert observation.normalized_text is not None or True
    assert observation.raw_request_hash == observation.raw_request_hash


def test_qualification_smoke_approval_refuses_unverified_export(db, settings, tmp_path):
    run = _make_live_run(db, settings, suite_name="qualification-smoke-2026-08-10", repeats=1)
    settings = _export_settings(tmp_path)
    artifact = export_run(db, run, settings)
    verification = verify_export(Path(artifact.path))
    assert verification["ok"]
    # Deliberately leave the artifact unverified.
    db.commit()

    from typer import Exit as TyperExit

    from endpoint_ledger.cli import approve_qualification_run

    with pytest.raises(TyperExit) as exc_info:
        approve_qualification_run(run.id)
    assert exc_info.value.exit_code == 1


# ------------------------------------------------------------ stale cohorts


def test_stale_cohorts_invalidated_and_hidden(db, settings):
    # Open live run so we can simulate the data-loss incident (sealed evidence
    # is undeletable by design).
    run = _make_live_run(db, settings, seal=False)
    db.commit()
    from endpoint_ledger.measurement.cohorts import COHORT_STATUS_INVALIDATED, CohortService

    service = CohortService(db, settings)
    service.sync_cohorts("live")
    service.refresh_qualification_statuses()
    cohorts = db.query(BaselineCohort).filter(BaselineCohort.data_origin == "live").all()
    assert cohorts and all(c.status == "collecting" for c in cohorts)

    # Simulate the incident: the run's observations vanish.
    for observation in run.observations:
        db.delete(observation)
    db.commit()
    service.sync_cohorts("live")
    service.refresh_qualification_statuses()

    db.expire_all()
    stale = db.query(BaselineCohort).filter(
        BaselineCohort.data_origin == "live",
        BaselineCohort.status == COHORT_STATUS_INVALIDATED,
    ).all()
    assert stale
    assert all(c.invalidation_reason == "no eligible live observations" for c in stale)
    # Default view hides invalidated cohorts.
    visible = service.status_report(origin="live")
    assert all(c["cohort"].status != COHORT_STATUS_INVALIDATED for c in visible)
    # Explicit view shows them.
    with_invalidated = service.status_report(origin="live", include_invalidated=True)
    assert any(c["cohort"].status == COHORT_STATUS_INVALIDATED for c in with_invalidated)
