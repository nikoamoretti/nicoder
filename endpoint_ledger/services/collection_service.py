"""Collection plan service: create, enable, control, and execute planned runs.

All state changes are idempotent where reasonable. The scheduler calls
``execute_due_planned_runs``; plan controls and the "Run One Additional
Collection Now" action are invoked from the dashboard. Manual runs reuse the
exact same execution lifecycle as scheduled runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import (
    BaselineCohort,
    CollectionPlan,
    ModelResolutionEvent,
    Observation,
    PlannedCollectionRun,
    ProviderEndpoint,
    RunAnnotation,
    SuiteRun,
)
from endpoint_ledger.measurement.cohorts import (
    COHORT_STATUS_QUALIFIED,
    CohortService,
)
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from endpoint_ledger.services import evidence_service, notifications
from endpoint_ledger.services.preflight_service import perform_preflight

PLAN_STATES = (
    "proposed",
    "enabled",
    "paused",
    "stopping",
    "disabled",
    "completed_qualified",
    "completed_unqualified",
    "blocked",
)

RUN_STATUSES = (
    "planned",
    "running",
    "blocked",
    "completed",
    "completed_with_errors",
    "failed",
    "missed",
    "skipped",
)

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual_run_now"
TRIGGER_QUALIFICATION_SMOKE = "qualification_smoke"

MANUAL_ALLOWED_STATES = ("enabled", "paused")


def _now() -> datetime:
    return datetime.now(UTC)


def create_plan(
    session: Session,
    settings: Settings | None = None,
    *,
    provider: str,
    endpoint_id: int,
    suite_name: str,
    cohort_fingerprint: str,
    repeats_per_probe: int,
    interval_hours: float = 12.0,
    maximum_total_cost_usd: float = 2.0,
    maximum_request_count: int | None = None,
    maximum_runs: int = 16,
    maximum_days: int = 9,
    qualification_run_id: int | None = None,
) -> CollectionPlan:
    """Create a proposed collection plan with its qualification policy snapshot."""
    settings = settings or get_settings()
    service = CohortService(session, settings)
    policy = service._qualification_policy()  # snapshot of the configured policy
    latest_reported = session.scalar(
        select(Observation.model_reported)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            SuiteRun.data_origin == "live",
            Observation.model_reported.is_not(None),
        )
        .order_by(Observation.completed_at.desc())
        .limit(1)
    )
    plan = CollectionPlan(
        provider=provider,
        endpoint_id=endpoint_id,
        suite_name=suite_name,
        cohort_fingerprint=cohort_fingerprint,
        model_reported_at_creation=latest_reported,
        repeats_per_probe=repeats_per_probe,
        timezone=settings.local_timezone,
        schedule={"interval_hours": interval_hours},
        qualification_policy=policy,
        maximum_total_cost_usd=maximum_total_cost_usd,
        maximum_request_count=maximum_request_count,
        maximum_runs=maximum_runs,
        maximum_end_at=_now() + timedelta(days=maximum_days),
        state="proposed",
        qualification_run_id=qualification_run_id,
    )
    session.add(plan)
    session.commit()
    return plan


def enable_plan(session: Session, plan_id: int) -> dict[str, Any]:
    plan = session.get(CollectionPlan, plan_id)
    if plan is None:
        return {"ok": False, "reason": "plan not found"}
    if plan.state == "enabled":
        return {"ok": True, "already": True}
    if plan.state != "proposed":
        return {"ok": False, "reason": f"plan state is {plan.state}; cannot enable"}
    plan.state = "enabled"
    plan.enabled_at = _now()
    session.commit()
    _schedule_next_run(session, plan)
    notifications.notify(
        session,
        notifications.CATEGORY_COLLECTION_ENABLED,
        "Collection enabled",
        message=f"plan {plan_id} enabled; next run scheduled",
        severity="info",
        collection_plan_id=plan_id,
    )
    return {"ok": True}


def pause_plan(session: Session, plan_id: int) -> dict[str, Any]:
    plan = session.get(CollectionPlan, plan_id)
    if plan is None or plan.state != "enabled":
        return {"ok": False, "reason": "plan is not enabled"}
    plan.state = "paused"
    plan.paused_at = _now()
    session.commit()
    notifications.notify(
        session,
        notifications.CATEGORY_COLLECTION_PAUSED,
        "Collection paused",
        message=f"plan {plan_id} paused",
        severity="info",
        collection_plan_id=plan_id,
    )
    return {"ok": True}


def resume_plan(session: Session, plan_id: int) -> dict[str, Any]:
    plan = session.get(CollectionPlan, plan_id)
    if plan is None or plan.state != "paused":
        return {"ok": False, "reason": "plan is not paused"}
    plan.state = "enabled"
    plan.paused_at = None
    session.commit()
    _schedule_next_run(session, plan)
    return {"ok": True}


def stop_after_current(session: Session, plan_id: int) -> dict[str, Any]:
    plan = session.get(CollectionPlan, plan_id)
    if plan is None or plan.state != "enabled":
        return {"ok": False, "reason": "plan is not enabled"}
    plan.state = "stopping"
    session.commit()
    return {"ok": True}


def disable_plan(session: Session, plan_id: int) -> dict[str, Any]:
    plan = session.get(CollectionPlan, plan_id)
    if plan is None or plan.state in ("disabled", "completed_qualified", "completed_unqualified"):
        return {"ok": False, "reason": "plan already finished"}
    plan.state = "disabled"
    plan.completed_at = _now()
    plan.stop_reason = "disabled by user"
    session.commit()
    notifications.notify(
        session,
        notifications.CATEGORY_COLLECTION_DISABLED,
        "Collection disabled",
        message=f"plan {plan_id} disabled",
        severity="warn",
        collection_plan_id=plan_id,
    )
    return {"ok": True}


def _schedule_next_run(session: Session, plan: CollectionPlan) -> PlannedCollectionRun | None:
    """Create the next planned run if under the plan's limits."""
    if plan.state not in ("enabled", "proposed"):
        return None
    completed_runs = [
        r for r in plan.planned_runs if r.status in ("completed", "failed", "blocked")
    ]
    if plan.maximum_runs is not None and len(completed_runs) >= plan.maximum_runs:
        return None
    interval = float(plan.schedule.get("interval_hours", 12.0))
    next_time = _now() + timedelta(hours=interval)
    existing = session.scalar(
        select(PlannedCollectionRun).where(
            PlannedCollectionRun.collection_plan_id == plan.id,
            PlannedCollectionRun.scheduled_for == next_time,
        )
    )
    if existing is not None:
        return existing
    planned = PlannedCollectionRun(
        collection_plan_id=plan.id,
        scheduled_for=next_time,
        status="planned",
    )
    session.add(planned)
    session.commit()
    return planned


def execute_due_planned_runs(session: Session, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Process every planned run that is due.

    Uses a PostgreSQL advisory lock per plan so only one executor processes a
    plan at a time; each planned run is claimed atomically before execution,
    preventing duplicate SuiteRuns.
    """
    settings = settings or get_settings()
    now = _now()
    due = session.scalars(
        select(PlannedCollectionRun).where(
            PlannedCollectionRun.status == "planned",
            PlannedCollectionRun.scheduled_for <= now,
        ).order_by(PlannedCollectionRun.scheduled_for)
    ).all()
    outcomes = []
    for planned in due:
        plan = session.get(CollectionPlan, planned.collection_plan_id)
        if plan is None:
            continue
        if planned.trigger_type != TRIGGER_MANUAL and plan.state != "enabled":
            continue
        # Per-plan advisory lock (released when the transaction ends).
        got_lock = session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": 10_000_000 + plan.id}
        ).scalar()
        if not got_lock:
            continue
        try:
            outcome = _execute_planned_run(session, settings, plan, planned, now)
            outcomes.append(outcome)
        finally:
            session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": 10_000_000 + plan.id})
            session.commit()
    return outcomes


def _execute_planned_run(
    session: Session,
    settings: Settings,
    plan: CollectionPlan,
    planned: PlannedCollectionRun,
    now: datetime,
) -> dict[str, Any]:
    """Execute one planned run through the full safe lifecycle."""
    is_manual = planned.trigger_type == TRIGGER_MANUAL
    planned.status = "running"
    planned.started_at = now
    planned.attempt_count += 1
    session.commit()
    if is_manual:
        notifications.notify(
            session,
            notifications.CATEGORY_MANUAL_STARTED,
            "Manual collection run started",
            message=f"manual run {planned.id} started executing",
            severity="info",
            collection_plan_id=plan.id,
            suite_run_id=planned.suite_run_id,
        )

    def _block(reason: str) -> dict[str, Any]:
        planned.status = "blocked"
        planned.blocking_reason = reason
        planned.completed_at = _now()
        session.commit()
        notifications.notify(
            session,
            notifications.CATEGORY_MANUAL_BLOCKED if is_manual else notifications.CATEGORY_RUN_BLOCKED,
            "Manual run blocked" if is_manual else "Scheduled run blocked",
            message=reason,
            severity="warn",
            collection_plan_id=plan.id,
            suite_run_id=planned.suite_run_id,
        )
        return {"ok": False, "planned_run_id": planned.id, "reason": reason}

    # Preconditions: manual runs may execute on an enabled or paused plan;
    # scheduled runs require the recurring schedule to be active.
    if is_manual:
        if plan.state not in MANUAL_ALLOWED_STATES:
            return _block(f"plan is {plan.state}")
    elif plan.state != "enabled":
        return _block(f"plan is {plan.state}")
    endpoint = session.get(ProviderEndpoint, plan.endpoint_id)
    if endpoint is None:
        return _block("endpoint not found")
    from endpoint_ledger.services.credentials import CredentialResolutionError, resolve_credential

    try:
        resolve_credential(session, plan.provider, settings)
    except CredentialResolutionError as exc:
        return _block(str(exc))

    if plan.maximum_end_at is not None and now > plan.maximum_end_at:
        plan.state = "completed_unqualified"
        plan.stop_reason = "maximum collection duration reached"
        plan.completed_at = _now()
        session.commit()
        return _block("maximum collection duration reached")

    # Preflight compatibility.
    report = perform_preflight(
        session, settings,
        provider=plan.provider, endpoint_filter=endpoint.model_requested,
        suite=plan.suite_name, repeats=plan.repeats_per_probe,
        cost_ceiling=plan.maximum_total_cost_usd or 0.0,
    )
    planned.configuration_hash = report.get("configuration_hash")
    session.commit()
    if not report["ready"]:
        return _block("; ".join(report["refusals"]))

    # Cost ceiling.
    estimate = report.get("conservative_max_cost_usd") or 0.0
    if plan.maximum_total_cost_usd is not None and plan.cumulative_cost_usd + estimate > plan.maximum_total_cost_usd:
        return _block("cost limit would be exceeded")

    # Recent verified backup when live evidence exists.
    from endpoint_ledger.operations import require_recent_verified_backup

    try:
        require_recent_verified_backup(session, settings)
    except RuntimeError as exc:
        return _block(str(exc))

    # Execute the live run.
    runner = SuiteRunner(session, settings)
    summary = runner.run(
        SuiteOptions(
            suite_name=plan.suite_name,
            provider=plan.provider,
            endpoint_filter=endpoint.model_requested,
            repeats=plan.repeats_per_probe,
            live=True,
            eligible_only=True,
        )
    )
    run = summary.suite_run
    if run is None:  # pragma: no cover
        return _block("no SuiteRun produced")
    planned.suite_run_id = run.id
    session.commit()

    # Seal, export, verify.
    export_result = evidence_service.seal_export_verify(session, run, settings)
    if not export_result.get("ok") and is_manual:
        notifications.notify(
            session,
            notifications.CATEGORY_MANUAL_EXPORT_FAILED,
            "Manual run evidence export failed",
            message="The sealed database evidence is preserved; the portable export failed. "
            f"Reason: {export_result.get('error', 'unknown')}",
            severity="error",
            collection_plan_id=plan.id,
            suite_run_id=run.id,
        )
    if run.actual_cost_usd is not None:
        plan.cumulative_cost_usd = round(plan.cumulative_cost_usd + run.actual_cost_usd, 6)
    session.commit()

    # Cohort update + qualification.
    cohort_service = CohortService(session, settings)
    cohort_service.sync_cohorts("live")
    cohort_service.refresh_qualification_statuses()

    # Model-resolution events.
    model_change = _detect_model_resolution(session, plan, run)
    if model_change:
        plan.state = "paused"
        plan.paused_at = _now()
        session.commit()
        notifications.notify(
            session,
            notifications.CATEGORY_MANUAL_MODEL_RESOLUTION if is_manual else notifications.CATEGORY_MODEL_RESOLUTION,
            "Provider-reported model changed",
            message=(
                f"requested {plan.model_reported_at_creation} now reports "
                f"{model_change.new_model_reported}; collection paused and a new cohort "
                "boundary is required. Review and approve continuation in Baseline Operations."
            ),
            severity="warn",
            collection_plan_id=plan.id,
            suite_run_id=run.id,
        )

    if run.status == "completed":
        planned.status = "completed"
    elif run.status == "completed_with_errors":
        planned.status = "completed_with_errors"
    else:
        planned.status = "failed"
    planned.completed_at = _now()
    session.commit()

    # Qualification evaluation.
    cohort = session.scalar(
        select(BaselineCohort).where(BaselineCohort.fingerprint == plan.cohort_fingerprint)
    )
    if cohort is not None and cohort.status == COHORT_STATUS_QUALIFIED:
        plan.state = "completed_qualified"
        plan.completed_at = _now()
        plan.stop_reason = "cohort qualified"
        session.commit()
        from endpoint_ledger.services.report_service import create_baseline_report

        create_baseline_report(session, cohort, settings)
        notifications.notify(
            session,
            notifications.CATEGORY_MANUAL_QUALIFIED if is_manual else notifications.CATEGORY_BASELINE_QUALIFIED,
            "Baseline qualified",
            message=f"cohort {plan.cohort_fingerprint[:12]}… qualified; plan stopped",
            severity="info",
            collection_plan_id=plan.id,
            suite_run_id=run.id,
        )
    else:
        if is_manual:
            if run.status == "completed_with_errors":
                notifications.notify(
                    session,
                    notifications.CATEGORY_MANUAL_PARTIAL_FAILURE,
                    "Manual collection run completed with errors",
                    message=f"manual run {planned.id} status={run.status}; successful observations preserved",
                    severity="warn",
                    collection_plan_id=plan.id,
                    suite_run_id=run.id,
                )
            else:
                notifications.notify(
                    session,
                    notifications.CATEGORY_MANUAL_COMPLETED,
                    "Manual collection run completed",
                    message=f"run {run.id} status={run.status}",
                    severity="info" if run.status == "completed" else "warn",
                    collection_plan_id=plan.id,
                    suite_run_id=run.id,
                )
        else:
            notifications.notify(
                session,
                notifications.CATEGORY_RUN_COMPLETED,
                "Scheduled run completed",
                message=f"run {run.id} status={run.status}",
                severity="info" if run.status == "completed" else "warn",
                collection_plan_id=plan.id,
                suite_run_id=run.id,
            )

    # A manual run never creates, shifts, or deletes the recurring schedule;
    # a paused plan stays paused, an enabled plan keeps its existing schedule.
    if not is_manual and plan.state == "enabled":
        _schedule_next_run(session, plan)
    elif plan.state == "stopping":
        plan.state = "disabled"
        plan.completed_at = _now()
        plan.stop_reason = "stop after current run"
        session.commit()

    return {"ok": True, "planned_run_id": planned.id, "suite_run_id": run.id}


def _detect_model_resolution(session: Session, plan: CollectionPlan, run: SuiteRun) -> ModelResolutionEvent | None:
    reported = [o.model_reported for o in run.observations if o.model_reported]
    if not reported:
        return None
    latest = reported[-1]
    endpoint = session.get(ProviderEndpoint, plan.endpoint_id)
    requested = endpoint.model_requested if endpoint else plan.model_reported_at_creation
    if plan.model_reported_at_creation and latest != plan.model_reported_at_creation:
        existing = session.scalar(
            select(ModelResolutionEvent).where(
                ModelResolutionEvent.requested_model == requested,
                ModelResolutionEvent.new_model_reported == latest,
            )
        )
        if existing is None:
            existing = ModelResolutionEvent(
                requested_model=requested,
                old_model_reported=plan.model_reported_at_creation,
                new_model_reported=latest,
                first_observed_at=_now(),
                event_type="model_alias_routing",
                notes="exact lifecycle/routing event, not a statistically inferred behavioral change",
            )
            session.add(existing)
            session.commit()
        return existing
    return None


def handle_missed_runs(session: Session) -> int:
    """Mark past-due planned runs of inactive plans as missed; never burst-run.

    Queued manual runs are requests, not schedule slots: they stay queued and
    are never silently converted to "missed".
    """
    now = _now()
    missed = session.scalars(
        select(PlannedCollectionRun).where(
            PlannedCollectionRun.status == "planned",
            PlannedCollectionRun.scheduled_for < now - timedelta(hours=1),
            PlannedCollectionRun.trigger_type != TRIGGER_MANUAL,
        )
    ).all()
    count = 0
    for planned in missed:
        planned.status = "missed"
        count += 1
        notifications.notify(
            session,
            notifications.CATEGORY_MISSED_RUN,
            "Missed scheduled run",
            message=f"planned run {planned.id} was missed while the application was offline",
            severity="warn",
            collection_plan_id=planned.collection_plan_id,
        )
    if count:
        session.commit()
    return count


# ---------------------------------------------------------------- manual run-now


def plan_cumulative_request_count(session: Session, plan_id: int) -> int:
    """Total expected live requests executed by this plan's completed runs.

    Includes scheduled and manual runs alike.
    """
    return (
        session.scalar(
            select(func.coalesce(func.sum(SuiteRun.total_expected_observations), 0))
            .join(PlannedCollectionRun, PlannedCollectionRun.suite_run_id == SuiteRun.id)
            .where(
                PlannedCollectionRun.collection_plan_id == plan_id,
                PlannedCollectionRun.status.in_(("completed", "completed_with_errors")),
            )
        )
        or 0
    )


def next_scheduled_run(session: Session, plan_id: int) -> PlannedCollectionRun | None:
    """The next not-yet-executed recurring scheduled run, if one exists."""
    return session.scalar(
        select(PlannedCollectionRun).where(
            PlannedCollectionRun.collection_plan_id == plan_id,
            PlannedCollectionRun.trigger_type == TRIGGER_SCHEDULED,
            PlannedCollectionRun.status == "planned",
        ).order_by(PlannedCollectionRun.scheduled_for).limit(1)
    )


def manual_run_checks(
    session: Session,
    settings: Settings,
    plan: CollectionPlan,
) -> dict[str, Any]:
    """Fresh preflight + plan-level eligibility for a manual collection run.

    Read-only except for persisting the fresh PreflightRecord. Returns the
    preflight report and a list of blocking reasons (empty = eligible).
    """
    blockers: list[str] = []
    endpoint = session.get(ProviderEndpoint, plan.endpoint_id)
    if endpoint is None:
        blockers.append("plan endpoint no longer exists")
    if plan.state not in MANUAL_ALLOWED_STATES:
        blockers.append(f"plan state is {plan.state}; manual run not permitted")

    if plan.qualification_run_id is not None:
        approved = session.scalar(
            select(RunAnnotation).where(
                RunAnnotation.suite_run_id == plan.qualification_run_id,
                RunAnnotation.annotation_type == "qualification_approval",
            )
        )
        if approved is None:
            blockers.append("qualification approval is missing or no longer valid")

    if endpoint is not None:
        unresolved_event = session.scalar(
            select(ModelResolutionEvent).where(
                ModelResolutionEvent.requested_model == endpoint.model_requested,
                ModelResolutionEvent.new_model_reported != plan.model_reported_at_creation,
            ).order_by(ModelResolutionEvent.first_observed_at.desc()).limit(1)
        )
        if unresolved_event is not None:
            blockers.append(
                "unresolved model-resolution event requires approval "
                f"(requested {endpoint.model_requested} now reports {unresolved_event.new_model_reported})"
            )

    running = session.scalar(
        select(PlannedCollectionRun.id).where(
            PlannedCollectionRun.collection_plan_id == plan.id,
            PlannedCollectionRun.status == "running",
        ).limit(1)
    )
    if running is not None:
        blockers.append("another collection run for this plan is currently executing")
    queued = session.scalar(
        select(PlannedCollectionRun.id).where(
            PlannedCollectionRun.collection_plan_id == plan.id,
            PlannedCollectionRun.trigger_type == TRIGGER_MANUAL,
            PlannedCollectionRun.status == "planned",
        ).limit(1)
    )
    if queued is not None:
        blockers.append("a manual run for this plan is already queued")

    report = perform_preflight(
        session, settings,
        provider=plan.provider,
        endpoint_filter=endpoint.model_requested if endpoint else "",
        suite=plan.suite_name,
        repeats=plan.repeats_per_probe,
        cost_ceiling=plan.maximum_total_cost_usd,
    )
    blockers.extend(report.get("refusals") or [])

    estimate = report.get("conservative_max_cost_usd") or 0.0
    expected = report.get("expected_request_count") or 0

    if plan.maximum_total_cost_usd is not None and plan.cumulative_cost_usd + estimate > plan.maximum_total_cost_usd:
        blockers.append("approved cost limit would be exceeded by this additional run")

    cumulative_requests = plan_cumulative_request_count(session, plan.id)
    if (
        plan.maximum_request_count is not None
        and cumulative_requests + expected > plan.maximum_request_count
    ):
        blockers.append("approved request-count limit would be exceeded by this additional run")

    if plan.maximum_end_at is not None and _now() > plan.maximum_end_at:
        blockers.append("maximum collection end timestamp has been reached")

    if report.get("probes"):
        cohort = session.scalar(
            select(BaselineCohort).where(
                BaselineCohort.fingerprint == plan.cohort_fingerprint,
                BaselineCohort.data_origin == "live",
            )
        )
        if cohort is None:
            blockers.append("cohort fingerprint is no longer compatible with the approved plan")
        elif cohort.status == "invalidated":
            blockers.append("the approved plan's cohort was invalidated")

    last_completed = session.scalar(
        select(PlannedCollectionRun).where(
            PlannedCollectionRun.collection_plan_id == plan.id,
            PlannedCollectionRun.status.in_(("completed", "completed_with_errors")),
            PlannedCollectionRun.configuration_hash.is_not(None),
        ).order_by(PlannedCollectionRun.completed_at.desc()).limit(1)
    )
    if last_completed is not None and report.get("configuration_hash") != last_completed.configuration_hash:
        blockers.append("configuration hash changed since the last completed run")

    return {
        "plan": plan,
        "endpoint": endpoint,
        "preflight": report,
        "blockers": blockers,
        "expected_request_count": expected,
        "maximum_incremental_cost_usd": estimate,
        "cumulative_request_count": cumulative_requests,
    }


def manual_run_confirmation(session: Session, settings: Settings, plan_id: int) -> dict[str, Any]:
    """Data for the "Run One Additional Collection Now" confirmation page."""
    settings = settings or get_settings()
    plan = session.get(CollectionPlan, plan_id)
    if plan is None:
        return {"ok": False, "blockers": ["plan not found"]}
    checks = manual_run_checks(session, settings, plan)
    latest_reported = session.scalar(
        select(Observation.model_reported)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == plan.endpoint_id,
            SuiteRun.data_origin == "live",
            Observation.model_reported.is_not(None),
        )
        .order_by(Observation.completed_at.desc())
        .limit(1)
    )
    from endpoint_ledger.backup import last_verified_backup

    backup = last_verified_backup(settings)
    checks["latest_model_reported"] = latest_reported
    checks["latest_verified_backup"] = backup
    checks["evidence_export_destination"] = str(settings.evidence_export_path)
    next_run = next_scheduled_run(session, plan_id)
    checks["next_scheduled_run"] = next_run
    checks["close_to_scheduled_run"] = bool(
        next_run is not None and next_run.scheduled_for - _now() < timedelta(hours=1)
    )
    checks["eligible_probes"] = [
        p for p in (checks["preflight"].get("probes") or []) if p.get("baseline_eligible")
    ]
    return checks


def request_manual_run(
    session: Session,
    settings: Settings | None,
    plan_id: int,
    idempotency_key: str,
) -> dict[str, Any]:
    """Enqueue one additional manual collection run (idempotent).

    The dashboard request only enqueues; the existing scheduler execution path
    runs the collection with the full safe lifecycle. Returns the existing
    PlannedCollectionRun when the same idempotency key is submitted again.
    """
    settings = settings or get_settings()
    if not idempotency_key or not idempotency_key.strip():
        return {"ok": False, "blockers": ["idempotency token missing"]}
    key = idempotency_key.strip()[:64]

    existing = session.scalar(
        select(PlannedCollectionRun).where(
            PlannedCollectionRun.idempotency_key == key
        ).order_by(PlannedCollectionRun.id.desc()).limit(1)
    )
    if existing is not None:
        return {"ok": True, "duplicate": True, "planned_run_id": existing.id}

    plan = session.get(CollectionPlan, plan_id)
    if plan is None:
        return {"ok": False, "blockers": ["plan not found"]}

    checks = manual_run_checks(session, settings, plan)
    if checks["blockers"]:
        notifications.notify(
            session,
            notifications.CATEGORY_MANUAL_BLOCKED,
            "Manual collection run blocked",
            message="; ".join(checks["blockers"]),
            severity="warn",
            collection_plan_id=plan.id,
        )
        return {"ok": False, "blockers": checks["blockers"]}

    now = _now()
    planned = PlannedCollectionRun(
        collection_plan_id=plan.id,
        scheduled_for=now,
        trigger_type=TRIGGER_MANUAL,
        idempotency_key=key,
        requested_at=now,
        requested_by="dashboard",
        cohort_fingerprint=plan.cohort_fingerprint,
        expected_request_count=checks["expected_request_count"],
        maximum_incremental_cost_usd=checks["maximum_incremental_cost_usd"],
        configuration_hash=checks["preflight"].get("configuration_hash"),
        status="planned",
    )
    session.add(planned)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(PlannedCollectionRun).where(
                PlannedCollectionRun.idempotency_key == key
            ).order_by(PlannedCollectionRun.id.desc()).limit(1)
        )
        if existing is not None:
            return {"ok": True, "duplicate": True, "planned_run_id": existing.id}
        raise
    notifications.notify(
        session,
        notifications.CATEGORY_MANUAL_REQUESTED,
        "Manual collection run requested",
        message=(
            f"one additional collection run queued for plan {plan.id}: "
            f"{checks['expected_request_count']} live requests, max incremental "
            f"cost ${checks['maximum_incremental_cost_usd']:.6f}"
        ),
        severity="info",
        collection_plan_id=plan.id,
    )
    return {"ok": True, "planned_run_id": planned.id}
