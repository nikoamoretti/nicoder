"""Qualification-smoke run service.

Creates, reviews, approves, or rejects a live qualification-smoke run. The
service performs no provider call itself except through the existing runner.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import (
    Observation,
    RunAnnotation,
    RunArtifact,
    Score,
    ScoreAdjudication,
    SuiteRun,
)
from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES
from endpoint_ledger.measurement.cohorts import CohortService, primary_outcome_for_observation
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from endpoint_ledger.services import evidence_service, notifications
from endpoint_ledger.services.preflight_service import perform_preflight


def smoke_suite_name(today: date | None = None) -> str:
    return f"qualification-smoke-{(today or date.today()).isoformat()}"


def execute_qualification_smoke(
    session: Session,
    settings: Settings | None = None,
    *,
    provider: str = "openai",
    endpoint_filter: str = "gpt-4o-mini",
    suite: str = "smoke",
    cost_ceiling: float = 1.0,
) -> dict[str, Any]:
    """Run a live qualification-smoke run. Idempotent per day per provider.

    If a smoke run for today already exists and is not failed, it is returned
    rather than creating a duplicate.
    """
    settings = settings or get_settings()
    suite_name = smoke_suite_name()

    existing = session.scalar(
        select(SuiteRun).where(
            SuiteRun.suite_name == suite_name,
            SuiteRun.data_origin == "live",
            SuiteRun.status != "failed",
        )
    )
    if existing is not None:
        return {"ok": True, "run_id": existing.id, "created": False, "duplicate": True}

    # Preflight must be ready.
    report = perform_preflight(
        session, settings,
        provider=provider, endpoint_filter=endpoint_filter,
        suite=suite, repeats=1, cost_ceiling=cost_ceiling,
    )
    if not report["ready"]:
        notifications.notify(
            session,
            notifications.CATEGORY_PREFLIGHT_BLOCKED,
            "Preflight blocked",
            message="; ".join(report["refusals"]),
            severity="warn",
        )
        return {"ok": False, "reason": "preflight_blocked", "refusals": report["refusals"]}

    # Ensure a recent verified backup when live evidence exists.
    from endpoint_ledger.operations import require_recent_verified_backup

    require_recent_verified_backup(session, settings)

    runner = SuiteRunner(session, settings)
    options = SuiteOptions(
        suite_name=suite_name,
        provider=provider,
        endpoint_filter=endpoint_filter,
        repeats=1,
        live=True,
        cost_ceiling_usd=cost_ceiling,
        eligible_only=True,
    )
    summary = runner.run(options)
    run = summary.suite_run
    if run is None:  # pragma: no cover
        return {"ok": False, "reason": "no_suite_run"}

    # Seal, export, verify evidence.
    export_result = evidence_service.seal_export_verify(session, run, settings)

    CohortService(session, settings).sync_cohorts("live")

    notifications.notify(
        session,
        notifications.CATEGORY_QUALIFICATION_SMOKE_COMPLETED,
        "Qualification-smoke run completed",
        message=f"run {run.id} status={run.status} export_ok={export_result['ok']}",
        severity="info" if export_result["ok"] else "error",
        suite_run_id=run.id,
    )
    notifications.notify(
        session,
        notifications.CATEGORY_QUALIFICATION_AWAITING_REVIEW,
        "Qualification-smoke run awaits review",
        message=f"run {run.id} needs manual review and approval",
        severity="info",
        suite_run_id=run.id,
    )
    return {
        "ok": True,
        "run_id": run.id,
        "created": True,
        "status": run.status,
        "export": export_result,
    }


def review_report(session: Session, run_id: int) -> dict[str, Any]:
    """Build the review summary for a qualification-smoke run."""
    run = session.get(SuiteRun, run_id)
    if run is None:
        raise ValueError(f"suite run {run_id} does not exist")
    observations = list(run.observations)
    per_probe: dict[str, dict[str, Any]] = {}
    for observation in observations:
        key = observation.probe_version.probe.stable_key
        entry = per_probe.setdefault(key, {"observations": 0, "primary_passed": None})
        entry["observations"] += 1
        passed, derived = primary_outcome_for_observation(observation)
        if passed is not None:
            entry["primary_passed"] = passed
        entry["primary_derived"] = derived
    model_reported = next(
        (o.model_reported for o in observations if o.model_reported is not None), None
    )
    from endpoint_ledger.db.models import RunArtifact

    artifact = session.scalar(
        select(RunArtifact).where(
            RunArtifact.suite_run_id == run_id, RunArtifact.artifact_type == "run_export"
        ).order_by(RunArtifact.id.desc())
    )
    return {
        "run": run,
        "observations": observations,
        "per_probe": per_probe,
        "model_reported": model_reported,
        "artifact": artifact,
        "manifest_hash": run.evidence_manifest_hash,
        "evidence_state": run.evidence_state,
    }


def approval_checks(session: Session, run_id: int) -> list[str]:
    """Refusal reasons for approving a qualification-smoke run (empty = OK)."""
    run = session.get(SuiteRun, run_id)
    if run is None:
        return [f"suite run {run_id} does not exist"]
    observations = list(run.observations)
    errors: list[str] = []
    if not observations:
        errors.append("run has no observations")
    if run.completed_observations < run.total_expected_observations:
        errors.append(
            f"missing observations ({run.completed_observations} of {run.total_expected_observations})"
        )
    if run.failed_observations > 0:
        failure_analysis = session.scalars(
            select(RunAnnotation).where(
                RunAnnotation.suite_run_id == run_id,
                RunAnnotation.annotation_type == "failure_analysis",
            )
        ).all()
        if not failure_analysis:
            errors.append("provider failures present but not analyzed (add a failure_analysis annotation)")

    probe_keys = {o.probe_version.probe.stable_key for o in observations}
    for key in sorted(probe_keys):
        probe_obs = [o for o in observations if o.probe_version.probe.stable_key == key]
        if not any(primary_outcome_for_observation(o)[0] is not None for o in probe_obs):
            errors.append(f"probe {key} has no primary score")

    defect_scores = session.scalars(
        select(ScoreAdjudication.id)
        .join(Score, Score.id == ScoreAdjudication.score_id)
        .join(Observation, Observation.id == Score.observation_id)
        .where(
            Observation.suite_run_id == run_id,
            ScoreAdjudication.classification.in_(sorted(MEASUREMENT_DEFECT_CLASSES)),
        )
    ).all()
    if defect_scores:
        errors.append(f"{len(defect_scores)} probe/scorer defect(s) recorded on this run")

    from pathlib import Path

    from endpoint_ledger.export import verify_export

    artifact = session.scalar(
        select(RunArtifact).where(
            RunArtifact.suite_run_id == run_id, RunArtifact.artifact_type == "run_export"
        ).order_by(RunArtifact.id.desc())
    )
    if artifact is None:
        errors.append("no evidence export exists for this run")
    else:
        verification = verify_export(Path(artifact.path))
        if not verification["ok"]:
            errors.append("evidence export does not verify")
        elif artifact.verification_status != "verified":
            errors.append("evidence export is not marked verified")

    if run.data_origin == "live":
        missing_reported = [
            o.id for o in observations if o.error_type is None and o.model_reported is None
        ]
        if missing_reported:
            errors.append(f"{len(missing_reported)} observation(s) lack provider-reported model")
    return errors


def approve(session: Session, run_id: int, reviewer_label: str = "dashboard") -> dict[str, Any]:
    """Approve a qualification-smoke run; adds a separate annotation."""
    errors = approval_checks(session, run_id)
    if errors:
        return {"ok": False, "errors": errors}
    existing = session.scalar(
        select(RunAnnotation).where(
            RunAnnotation.suite_run_id == run_id,
            RunAnnotation.annotation_type == "qualification_approval",
        )
    )
    if existing is not None:
        return {"ok": True, "already_approved": True}
    session.add(
        RunAnnotation(
            suite_run_id=run_id,
            annotation_type="qualification_approval",
            notes="qualification-smoke run approved for baseline collection",
            reviewer_label=reviewer_label,
        )
    )
    session.commit()
    notifications.notify(
        session,
        notifications.CATEGORY_QUALIFICATION_APPROVED,
        "Qualification run approved",
        message=f"run {run_id} approved; baseline collection may be scheduled",
        severity="info",
        suite_run_id=run_id,
    )
    return {"ok": True, "already_approved": False}


def reject(session: Session, run_id: int, reason: str, reviewer_label: str = "dashboard") -> dict[str, Any]:
    """Reject a qualification-smoke run; creates a measurement issue."""
    if not reason or not reason.strip():
        return {"ok": False, "errors": ["a rejection reason is required"]}
    session.add(
        RunAnnotation(
            suite_run_id=run_id,
            annotation_type="qualification_rejection",
            notes=reason.strip(),
            reviewer_label=reviewer_label,
        )
    )
    session.commit()
    notifications.notify(
        session,
        notifications.CATEGORY_QUALIFICATION_REJECTED,
        "Qualification run rejected",
        message=f"run {run_id} rejected: {reason.strip()}",
        severity="warn",
        suite_run_id=run_id,
    )
    return {"ok": True}
