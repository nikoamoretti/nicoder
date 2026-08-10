"""Server-rendered dashboard routes.

Every number is computed from the database at request time. Live data is the
default; other origins (mock, synthetic, demo, test) are opt-in filters.
Behavioral summaries use primary outcomes only; diagnostics explain failures
and never receive independent weight.
"""

from __future__ import annotations

import statistics
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from endpoint_ledger.db.models import (
    ChangeEvent,
    Observation,
    Probe,
    ProbeVersion,
    ProviderEndpoint,
    Score,
    ScoreAdjudication,
    SuiteRun,
)
from endpoint_ledger.measurement.cohorts import (
    CohortService,
    cohort_dimensions,
    fingerprint_for_dimensions,
    primary_outcome_for_observation,
)
from endpoint_ledger.measurement.origins import DATA_ORIGINS, LIVE_ORIGIN
from endpoint_ledger.measurement.stats import wilson_interval

router = APIRouter()

DEFAULT_ORIGIN = LIVE_ORIGIN


def get_db(request: Request):
    factory = request.app.state.session_factory
    db: Session = factory()
    try:
        yield db
    finally:
        db.close()


def _templates(request: Request):
    return request.app.state.templates


def _origin_param(request: Request) -> str:
    origin = request.query_params.get("origin", DEFAULT_ORIGIN)
    if origin not in DATA_ORIGINS and origin != "all":
        origin = DEFAULT_ORIGIN
    return origin


def _origin_filter(origin: str) -> Any:
    if origin == "all":
        return SuiteRun.data_origin.in_(list(DATA_ORIGINS))
    return SuiteRun.data_origin == origin


def _event_origin_filter(origin: str) -> Any:
    if origin == "all":
        return ChangeEvent.data_origin.in_(list(DATA_ORIGINS))
    return ChangeEvent.data_origin == origin


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    templates = _templates(request)
    origin = _origin_param(request)

    origin_filter = _origin_filter(origin)
    enabled_endpoints = db.scalar(
        select(func.count()).select_from(ProviderEndpoint).where(ProviderEndpoint.enabled.is_(True))
    )
    observation_count = db.scalar(
        select(func.count())
        .select_from(Observation)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(origin_filter)
    )
    last_run = db.scalar(
        select(SuiteRun).where(origin_filter).order_by(SuiteRun.started_at.desc())
    )
    open_events = db.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.status.in_(["provisional", "confirmed"]), _event_origin_filter(origin))
        .order_by(ChangeEvent.detected_at.desc())
        .limit(10)
    ).all()

    category_reliability = _primary_reliability_by_category(db, origin)
    latest_median_latency: float | None = None
    recent_failures: list[Observation] = []
    if last_run is not None:
        latest_median_latency = _median_latency_for_run(db, last_run.id, origin)
        recent_failures = list(
            db.scalars(
                select(Observation)
                .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
                .where(
                    origin_filter,
                    Observation.suite_run_id == last_run.id,
                    Observation.error_type.is_not(None),
                )
                .order_by(Observation.requested_at.desc())
                .limit(8)
            ).all()
        )

    cohort_service = CohortService(db, settings)
    cohort_reports = cohort_service.status_report(origin=origin) if origin == LIVE_ORIGIN else []
    invalidated_reports = (
        cohort_service.status_report(origin=origin, include_invalidated=True)
        if origin == LIVE_ORIGIN
        else []
    )
    invalidated_count = sum(
        1 for r in invalidated_reports if r["cohort"].status == "invalidated"
    )
    unresolved_issues = _unresolved_issue_count(db, origin)
    account_incidents = _recent_incidents(db, origin, category="account_configuration", limit=5)
    transport_incidents = _recent_incidents(db, origin, category="provider_transport", limit=5)

    from endpoint_ledger.backup import last_verified_backup

    verified_backup = last_verified_backup(request.app.state.settings)

    sealed_not_exported = db.scalar(
        select(func.count())
        .select_from(SuiteRun)
        .where(origin_filter, SuiteRun.evidence_state == "sealed")
    )
    approval_status = _qualification_approval_status(db, origin_filter)

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "origin": origin,
            "origins": DATA_ORIGINS,
            "enabled_endpoints": enabled_endpoints,
            "observation_count": observation_count,
            "last_run": last_run,
            "open_events": open_events,
            "category_reliability": category_reliability,
            "latest_median_latency": latest_median_latency,
            "recent_failures": recent_failures,
            "cohort_reports": cohort_reports,
            "invalidated_count": invalidated_count,
            "unresolved_issues": unresolved_issues,
            "account_incidents": account_incidents,
            "transport_incidents": transport_incidents,
            "verified_backup": verified_backup,
            "sealed_not_exported": sealed_not_exported,
            "approval_status": approval_status,
            "settings": settings,
            "show_raw": settings.show_raw_responses,
        },
    )


def _qualification_approval_status(db: Session, origin_filter: Any) -> dict[str, Any]:
    from endpoint_ledger.db.models import RunAnnotation

    smoke_runs = list(
        db.scalars(
            select(SuiteRun)
            .where(origin_filter, SuiteRun.suite_name.like("qualification-smoke-%"))
            .order_by(SuiteRun.started_at.desc())
        ).all()
    )
    approved: list[int] = []
    pending: list[int] = []
    for run in smoke_runs:
        has_approval = db.scalar(
            select(RunAnnotation.id).where(
                RunAnnotation.suite_run_id == run.id,
                RunAnnotation.annotation_type == "qualification_approval",
            ).limit(1)
        )
        (approved if has_approval else pending).append(run.id)
    return {"approved": approved, "pending": pending}


@router.get("/endpoints/{endpoint_id}", response_class=HTMLResponse)
def endpoint_detail(request: Request, endpoint_id: int, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    templates = _templates(request)
    origin = _origin_param(request)
    origin_filter = _origin_filter(origin)

    endpoint = db.get(ProviderEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="endpoint not found")

    primary_stats = _primary_summary(db, endpoint_id, origin)
    diagnostic_stats = _diagnostic_success_rates(db, endpoint_id, origin)
    median_latency = _median_latency_for_endpoint(db, endpoint_id, origin)

    recent_observations = list(
        db.scalars(
            select(Observation)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(Observation.endpoint_id == endpoint_id, origin_filter)
            .order_by(Observation.requested_at.desc())
            .limit(20)
        ).all()
    )
    recent_failures = list(
        db.scalars(
            select(Observation)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                Observation.endpoint_id == endpoint_id,
                origin_filter,
                Observation.error_type.is_not(None),
            )
            .order_by(Observation.requested_at.desc())
            .limit(10)
        ).all()
    )
    events = db.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.endpoint_id == endpoint_id, _event_origin_filter(origin))
        .order_by(ChangeEvent.detected_at.desc())
    ).all()

    latency_rows = db.execute(
        select(Observation.completed_at, Observation.latency_ms)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            origin_filter,
            Observation.latency_ms.is_not(None),
        )
        .order_by(Observation.completed_at.asc())
    ).all()
    token_cost_rows = db.execute(
        select(
            Observation.requested_at,
            Observation.input_tokens,
            Observation.output_tokens,
            Observation.estimated_cost_usd,
        )
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(Observation.endpoint_id == endpoint_id, origin_filter)
        .order_by(Observation.requested_at.asc())
    ).all()

    return templates.TemplateResponse(
        request,
        "endpoint.html",
        {
            "origin": origin,
            "origins": DATA_ORIGINS,
            "endpoint": endpoint,
            "primary_stats": primary_stats,
            "diagnostic_stats": diagnostic_stats,
            "median_latency": median_latency,
            "recent_observations": recent_observations,
            "recent_failures": recent_failures,
            "events": events,
            "latency_history": latency_rows,
            "token_cost_history": token_cost_rows,
            "show_raw": settings.show_raw_responses,
        },
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    templates = _templates(request)
    run = db.get(SuiteRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    observations = list(
        db.scalars(
            select(Observation)
            .where(Observation.suite_run_id == run_id)
            .order_by(Observation.requested_at.asc())
        ).all()
    )
    endpoints = db.execute(
        select(ProviderEndpoint.id, ProviderEndpoint.display_name)
        .join(Observation, Observation.endpoint_id == ProviderEndpoint.id)
        .where(Observation.suite_run_id == run_id)
        .distinct()
    ).all()
    probes = db.execute(
        select(Probe.id, Probe.stable_key)
        .join(ProbeVersion, ProbeVersion.probe_id == Probe.id)
        .join(Observation, Observation.probe_version_id == ProbeVersion.id)
        .where(Observation.suite_run_id == run_id)
        .distinct()
    ).all()

    transport_success = sum(1 for o in observations if o.error_type is None)
    primary_rows = []
    for observation in observations:
        passed, derived = primary_outcome_for_observation(observation)
        primary_rows.append((observation, passed, derived))
    primary_pass = sum(1 for _, passed, _ in primary_rows if passed is True)
    primary_eligible = sum(1 for _, passed, _ in primary_rows if passed is not None)

    raw_observation_count = len(observations)
    distinct_runs = 1
    distinct_dates = len({o.completed_at.date() for o in observations if o.completed_at})
    if run.data_origin == "live":
        distinct_runs = db.scalar(
            select(func.count(func.distinct(Observation.suite_run_id)))
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                Observation.endpoint_id.in_(
                    select(Observation.endpoint_id).where(Observation.suite_run_id == run.id)
                ),
                SuiteRun.data_origin == "live",
                Observation.probe_version_id.in_(
                    select(Observation.probe_version_id).where(Observation.suite_run_id == run.id)
                ),
            )
        ) or 1

    diagnostics = _diagnostic_stats_for_observations(db, observations)
    from endpoint_ledger.db.models import PlannedCollectionRun, RunAnnotation

    annotations = list(
        db.scalars(
            select(RunAnnotation).where(RunAnnotation.suite_run_id == run_id).order_by(RunAnnotation.created_at)
        ).all()
    )
    planned_trigger = db.scalar(
        select(PlannedCollectionRun.trigger_type).where(
            PlannedCollectionRun.suite_run_id == run_id
        ).limit(1)
    )

    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run,
            "observations": observations,
            "primary_rows": primary_rows,
            "transport_success": transport_success,
            "primary_pass": primary_pass,
            "primary_eligible": primary_eligible,
            "raw_observation_count": raw_observation_count,
            "distinct_runs": distinct_runs,
            "distinct_dates": distinct_dates,
            "diagnostics": diagnostics,
            "annotations": annotations,
            "endpoints": endpoints,
            "probes": probes,
            "planned_trigger": planned_trigger,
            "show_raw": settings.show_raw_responses,
        },
    )


@router.get("/probes/{probe_id}", response_class=HTMLResponse)
def probe_detail(request: Request, probe_id: int, db: Session = Depends(get_db)):
    templates = _templates(request)
    origin = _origin_param(request)
    probe = db.get(Probe, probe_id)
    if probe is None:
        raise HTTPException(status_code=404, detail="probe not found")

    by_endpoint: list[dict[str, Any]] = []
    for endpoint in db.scalars(
        select(ProviderEndpoint).where(ProviderEndpoint.enabled.is_(True)).order_by(ProviderEndpoint.id)
    ).all():
        stats = _primary_summary(db, endpoint.id, origin, probe_id=probe_id)
        if stats:
            by_endpoint.append({"endpoint": endpoint, "stats": stats})

    historical: list[dict[str, Any]] = []
    for version in probe.versions:
        total = db.scalar(
            select(func.count()).select_from(Score)
            .join(Observation, Observation.id == Score.observation_id)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                Observation.probe_version_id == version.id,
                Score.passed.is_not(None),
                _origin_filter(origin),
            )
        ) or 0
        passed = db.scalar(
            select(func.count()).select_from(Score)
            .join(Observation, Observation.id == Score.observation_id)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                Observation.probe_version_id == version.id,
                Score.passed.is_(True),
                _origin_filter(origin),
            )
        ) or 0
        historical.append(
            {
                "version": version,
                "observation_count": len(version.observations),
                "score_count": total,
                "pass_rate": round(100.0 * passed / total, 1) if total else None,
            }
        )

    known_issues = list(
        db.scalars(
            select(ScoreAdjudication)
            .join(Score, Score.id == ScoreAdjudication.score_id)
            .join(Observation, Observation.id == Score.observation_id)
            .where(Observation.probe_version_id.in_([v.id for v in probe.versions]))
            .order_by(ScoreAdjudication.created_at.desc())
            .limit(20)
        ).all()
    )

    return templates.TemplateResponse(
        request,
        "probe.html",
        {
            "origin": origin,
            "origins": DATA_ORIGINS,
            "probe": probe,
            "by_endpoint": by_endpoint,
            "historical": historical,
            "known_issues": known_issues,
        },
    )


@router.get("/observations/{observation_id}", response_class=HTMLResponse)
def observation_detail(request: Request, observation_id: int, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    templates = _templates(request)
    observation = db.get(Observation, observation_id)
    if observation is None:
        raise HTTPException(status_code=404, detail="observation not found")
    scores = list(
        db.scalars(
            select(Score).where(Score.observation_id == observation_id).order_by(Score.scorer_name)
        ).all()
    )
    adjudications = list(
        db.scalars(
            select(ScoreAdjudication)
            .join(Score, Score.id == ScoreAdjudication.score_id)
            .where(Score.observation_id == observation_id)
            .order_by(ScoreAdjudication.created_at)
        ).all()
    )
    primary_passed, primary_derived = primary_outcome_for_observation(observation)
    fingerprint: str | None = None
    try:
        dimensions = cohort_dimensions(settings, observation.endpoint, observation.probe_version, observation)
        fingerprint = fingerprint_for_dimensions(dimensions)
    except Exception:  # pragma: no cover - defensive; missing relationships
        fingerprint = None

    return templates.TemplateResponse(
        request,
        "observation.html",
        {
            "observation": observation,
            "scores": scores,
            "adjudications": adjudications,
            "primary_passed": primary_passed,
            "primary_derived": primary_derived,
            "cohort_fingerprint": fingerprint,
            "show_raw": settings.show_raw_responses,
        },
    )


@router.get("/changes", response_class=HTMLResponse)
def changes(request: Request, db: Session = Depends(get_db)):
    templates = _templates(request)
    origin = _origin_param(request)
    events = db.scalars(
        select(ChangeEvent)
        .where(_event_origin_filter(origin))
        .order_by(ChangeEvent.detected_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "changes.html",
        {"origin": origin, "origins": DATA_ORIGINS, "events": events},
    )


# --------------------------------------------------------------------- helpers


def _primary_outcome_rows(db: Session, origin: str, endpoint_id: int | None = None, probe_id: int | None = None):
    query = (
        select(Observation)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .join(ProbeVersion, ProbeVersion.id == Observation.probe_version_id)
        .join(Probe, Probe.id == ProbeVersion.probe_id)
        .where(
            _origin_filter(origin),
            Observation.error_type.is_(None),
        )
    )
    if endpoint_id is not None:
        query = query.where(Observation.endpoint_id == endpoint_id)
    if probe_id is not None:
        query = query.where(ProbeVersion.probe_id == probe_id)
    observations = db.scalars(query.order_by(Observation.completed_at)).all()
    return [
        (o, o.probe_version, primary_outcome_for_observation(o)[0])
        for o in observations
        if primary_outcome_for_observation(o)[0] is not None
    ]


def _primary_summary(db: Session, endpoint_id: int, origin: str, probe_id: int | None = None) -> list[dict[str, Any]]:
    rows = _primary_outcome_rows(db, origin, endpoint_id=endpoint_id, probe_id=probe_id)
    grouped: dict[tuple[int, int], list[bool]] = {}
    first_last: dict[tuple[int, int], tuple[Any, Any]] = {}
    runs: dict[tuple[int, int], set[int]] = {}
    for observation, version, passed in rows:
        key = (version.id, observation.endpoint_id)
        grouped.setdefault(key, []).append(bool(passed))
        runs.setdefault(key, set()).add(observation.suite_run_id)
        first_last.setdefault(key, (observation.completed_at, observation.completed_at))
        prev = first_last[key]
        first_last[key] = (min(prev[0], observation.completed_at), max(prev[1], observation.completed_at))

    result: list[dict[str, Any]] = []
    cohort_service = CohortService(db)
    for (version_id, ep_id), values in sorted(grouped.items()):
        version = db.get(ProbeVersion, version_id)
        if version is None:  # pragma: no cover
            continue
        successes = sum(1 for v in values if v)
        total = len(values)
        interval = wilson_interval(successes, total)
        cohort = None
        if origin == LIVE_ORIGIN:
            try:
                reports = cohort_service.status_report(origin="live")
                for report in reports:
                    if report["cohort"].probe_version_id == version_id and report["cohort"].endpoint_id == ep_id:
                        cohort = report
                        break
            except Exception:  # pragma: no cover - defensive
                cohort = None
        first, last = first_last[(version_id, ep_id)]
        result.append(
            {
                "version": version,
                "successes": successes,
                "total": total,
                "rate": round(100.0 * successes / total, 1),
                "interval": interval,
                "distinct_runs": len(runs[(version_id, ep_id)]),
                "first_at": first,
                "last_at": last,
                "cohort_status": cohort["progress"]["status"] if cohort else None,
            }
        )
    return result


def _primary_reliability_by_category(db: Session, origin: str) -> list[dict[str, Any]]:
    rows = _primary_outcome_rows(db, origin)
    grouped: dict[str, list[bool]] = {}
    for _observation, version, passed in rows:
        grouped.setdefault(version.probe.category, []).append(bool(passed))
    result = []
    for category, values in sorted(grouped.items()):
        successes = sum(1 for v in values if v)
        total = len(values)
        result.append(
            {
                "category": category,
                "successes": successes,
                "total": total,
                "rate": round(100.0 * successes / total, 1),
                "interval": wilson_interval(successes, total),
            }
        )
    return result


def _diagnostic_success_rates(db: Session, endpoint_id: int, origin: str) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Score.metric_name, Score.passed, Score.role)
        .join(Observation, Observation.id == Score.observation_id)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            _origin_filter(origin),
            Score.passed.is_not(None),
            Score.role == "diagnostic",
        )
    ).all()
    grouped: dict[str, list[bool]] = {}
    for metric, passed, _role in rows:
        grouped.setdefault(metric, []).append(bool(passed))
    result = []
    for metric, values in sorted(grouped.items()):
        successes = sum(1 for v in values if v)
        result.append(
            {
                "metric": metric,
                "successes": successes,
                "total": len(values),
                "rate": round(100.0 * successes / len(values), 1),
            }
        )
    return result


def _diagnostic_stats_for_observations(db: Session, observations: list[Observation]) -> list[dict[str, Any]]:
    ids = [o.id for o in observations]
    if not ids:
        return []
    rows = db.execute(
        select(Score.metric_name, Score.passed)
        .where(Score.observation_id.in_(ids), Score.passed.is_not(None), Score.role == "diagnostic")
    ).all()
    grouped: dict[str, list[bool]] = {}
    for metric, passed in rows:
        grouped.setdefault(metric, []).append(bool(passed))
    result = []
    for metric, values in sorted(grouped.items()):
        successes = sum(1 for v in values if v)
        result.append(
            {"metric": metric, "successes": successes, "total": len(values),
             "rate": round(100.0 * successes / len(values), 1)}
        )
    return result


def _median_latency_for_run(db: Session, run_id: int, origin: str) -> float | None:
    latencies = list(
        db.scalars(
            select(Observation.latency_ms)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                Observation.suite_run_id == run_id,
                _origin_filter(origin),
                Observation.latency_ms.is_not(None),
            )
        ).all()
    )
    values = [float(x) for x in latencies if x is not None]
    if not values:
        return None
    return round(statistics.median(values), 1)


def _median_latency_for_endpoint(db: Session, endpoint_id: int, origin: str) -> float | None:
    latencies = list(
        db.scalars(
            select(Observation.latency_ms)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                Observation.endpoint_id == endpoint_id,
                _origin_filter(origin),
                Observation.latency_ms.is_not(None),
            )
        ).all()
    )
    values = [float(x) for x in latencies if x is not None]
    if not values:
        return None
    return round(statistics.median(values), 1)


def _unresolved_issue_count(db: Session, origin: str) -> int:
    from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES

    return len(
        db.execute(
            select(ScoreAdjudication.id)
            .join(Score, Score.id == ScoreAdjudication.score_id)
            .join(Observation, Observation.id == Score.observation_id)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                _origin_filter(origin),
                ScoreAdjudication.classification.in_(sorted(MEASUREMENT_DEFECT_CLASSES)),
            )
        ).all()
    )


def _recent_incidents(db: Session, origin: str, category: str, limit: int = 5) -> list[Observation]:
    return list(
        db.scalars(
            select(Observation)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                _origin_filter(origin),
                Observation.error_category == category,
            )
            .order_by(Observation.requested_at.desc())
            .limit(limit)
        ).all()
    )
