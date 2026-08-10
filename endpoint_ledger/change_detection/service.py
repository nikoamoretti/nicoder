"""Conservative change detection with measurement-integrity guards.

Phase 1.1 rules:

- Behavioral change claims are made only for *primary* outcome metrics of
  baseline-eligible probe versions, and only when the matching baseline cohort
  is qualified, all comparison dimensions match, and no measurement defect is
  unresolved. Otherwise the detector *refuses* and records why.
- Latency is informational: compared by medians, never a confirmed event in
  Phase 1.
- Live data is the default; mock/demo/test detection runs through an explicit
  non-live origin so tests and demos keep working without contaminating live
  claims.
- Nothing is ever described as confirmed based on a single changed response.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import scipy.stats as stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import (
    ChangeEvent,
    Observation,
    Probe,
    ProbeVersion,
    Score,
    SuiteRun,
)
from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES
from endpoint_ledger.measurement.cohorts import (
    CohortService,
    cohort_dimensions,
    fingerprint_for_dimensions,
)
from endpoint_ledger.measurement.multiple_comparisons import benjamini_hochberg
from endpoint_ledger.measurement.origins import LIVE_ORIGIN, DataOrigin

LATENCY_METRIC = "latency_measurement"


@dataclass
class WindowSample:
    passed: bool | None
    numeric_value: float | None
    completed_at: datetime


@dataclass
class Comparison:
    metric_name: str
    probe_category: str | None
    kind: str
    baseline_value: float | None
    current_value: float | None
    absolute_change: float | None
    relative_change: float | None
    p_value: float | None
    baseline_n: int
    current_n: int
    direction: str | None
    qualifies: bool
    reason: str


@dataclass
class DetectionRefusal:
    endpoint_id: int
    metric_name: str
    reason: str


@dataclass
class DetectionResult:
    events: list[ChangeEvent] = field(default_factory=list)
    refusals: list[DetectionRefusal] = field(default_factory=list)


def compare_binary(
    metric_name: str,
    probe_category: str | None,
    baseline: list[WindowSample],
    current: list[WindowSample],
    min_observations: int,
    min_absolute_change: float,
    alpha: float,
) -> Comparison:
    """Compare success rates between two windows with Fisher's exact test."""
    baseline_vals = [s.passed for s in baseline]
    current_vals = [s.passed for s in current]
    if any(v is None for v in baseline_vals + current_vals):
        return Comparison(
            metric_name=metric_name, probe_category=probe_category, kind="binary",
            baseline_value=None, current_value=None, absolute_change=None,
            relative_change=None, p_value=None, baseline_n=len(baseline),
            current_n=len(current), direction=None, qualifies=False,
            reason="some samples have no boolean pass/fail value",
        )
    n_b, n_c = len(baseline_vals), len(current_vals)
    if n_b < min_observations or n_c < min_observations:
        return Comparison(
            metric_name=metric_name, probe_category=probe_category, kind="binary",
            baseline_value=None, current_value=None, absolute_change=None,
            relative_change=None, p_value=None, baseline_n=n_b, current_n=n_c,
            direction=None, qualifies=False,
            reason=f"insufficient samples ({n_b} baseline / {n_c} current; need {min_observations} each)",
        )
    pass_b = sum(1 for v in baseline_vals if v)
    pass_c = sum(1 for v in current_vals if v)
    fail_b = n_b - pass_b
    fail_c = n_c - pass_c
    base_rate = pass_b / n_b
    curr_rate = pass_c / n_c
    abs_change = curr_rate - base_rate
    rel_change = (abs_change / base_rate) if base_rate else None
    p_value = float(stats.fisher_exact([[pass_b, fail_b], [pass_c, fail_c]])[1])
    direction = "regression" if abs_change < 0 else "improvement"
    qualifies = abs(abs_change) >= min_absolute_change and p_value < alpha
    if not qualifies:
        reason = (
            f"no material change (abs change {abs(abs_change):.3f} < threshold "
            f"{min_absolute_change} or p={p_value:.4f} >= {alpha})"
        )
    else:
        reason = (
            f"material change detected: success rate {base_rate:.3f} -> {curr_rate:.3f}, "
            f"p={p_value:.4f}"
        )
    return Comparison(
        metric_name=metric_name, probe_category=probe_category, kind="binary",
        baseline_value=round(base_rate, 4), current_value=round(curr_rate, 4),
        absolute_change=round(abs_change, 4), relative_change=(
            round(rel_change, 4) if rel_change is not None else None
        ),
        p_value=p_value, baseline_n=n_b, current_n=n_c, direction=direction,
        qualifies=qualifies, reason=reason,
    )


def compare_latency(
    metric_name: str,
    probe_category: str | None,
    baseline: list[WindowSample],
    current: list[WindowSample],
    min_observations: int,
    min_absolute_change: float,
    alpha: float,
) -> Comparison:
    """Compare median latency between two windows."""
    base_vals = [s.numeric_value for s in baseline if s.numeric_value is not None]
    curr_vals = [s.numeric_value for s in current if s.numeric_value is not None]
    n_b, n_c = len(base_vals), len(curr_vals)
    if n_b < min_observations or n_c < min_observations:
        return Comparison(
            metric_name=metric_name, probe_category=probe_category, kind="latency",
            baseline_value=None, current_value=None, absolute_change=None,
            relative_change=None, p_value=None, baseline_n=n_b, current_n=n_c,
            direction=None, qualifies=False,
            reason=f"insufficient samples ({n_b} baseline / {n_c} current; need {min_observations} each)",
        )
    median_b = statistics.median(base_vals)
    median_c = statistics.median(curr_vals)
    abs_change = median_c - median_b
    rel_change = (abs_change / median_b) if median_b else None
    p_value: float | None = None
    direction = "slower" if abs_change > 0 else ("faster" if abs_change < 0 else "unchanged")
    qualifies = abs(abs_change) >= min_absolute_change
    try:
        p_value = float(
            stats.mannwhitneyu(base_vals, curr_vals, alternative="two-sided").pvalue
        )
    except ValueError:  # pragma: no cover - both windows guaranteed non-empty
        p_value = None
    reason = (
        f"median latency {median_b:.1f}ms -> {median_c:.1f}ms "
        f"(abs {abs_change:.1f}ms); p={p_value:.4f}" if p_value is not None
        else f"median latency {median_b:.1f}ms -> {median_c:.1f}ms (abs {abs_change:.1f}ms)"
    )
    if not qualifies:
        reason = f"{reason}; below change threshold {min_absolute_change}ms"
    return Comparison(
        metric_name=metric_name, probe_category=probe_category, kind="latency",
        baseline_value=round(median_b, 1), current_value=round(median_c, 1),
        absolute_change=round(abs_change, 1), relative_change=(
            round(rel_change, 4) if rel_change is not None else None
        ),
        p_value=p_value, baseline_n=len(base_vals), current_n=len(curr_vals),
        direction=direction, qualifies=qualifies, reason=reason,
    )


def _load_samples(
    session: Session,
    endpoint_id: int,
    metric_name: str,
    split_after: datetime,
    origin: DataOrigin,
    probe_version_id: int | None = None,
) -> tuple[list[WindowSample], list[WindowSample], dict[str, Any]]:
    query = (
        select(Score, Observation, ProbeVersion, Probe, SuiteRun)
        .join(Observation, Observation.id == Score.observation_id)
        .join(ProbeVersion, ProbeVersion.id == Observation.probe_version_id)
        .join(Probe, Probe.id == ProbeVersion.probe_id)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            Score.metric_name == metric_name,
            SuiteRun.data_origin == origin,
            Score.passed.is_not(None) if metric_name != LATENCY_METRIC else Score.numeric_value.is_not(None),
        )
    )
    if probe_version_id is not None:
        query = query.where(Observation.probe_version_id == probe_version_id)
    rows = session.execute(query).all()
    baseline: list[WindowSample] = []
    current: list[WindowSample] = []
    for score, observation, _version, _probe, _suite in rows:
        completed_at = observation.completed_at or observation.requested_at
        sample = WindowSample(
            passed=score.passed,
            numeric_value=score.numeric_value,
            completed_at=completed_at,
        )
        if completed_at is not None and completed_at > split_after:
            current.append(sample)
        else:
            baseline.append(sample)
    if baseline:
        baseline_start = min(s.completed_at for s in baseline)
        baseline_end = max(s.completed_at for s in baseline)
    else:
        baseline_start = baseline_end = split_after
    if current:
        current_start = min(s.completed_at for s in current)
        current_end = max(s.completed_at for s in current)
    else:
        current_start = current_end = split_after
    window_bounds = {
        "baseline_start": baseline_start.isoformat(),
        "baseline_end": baseline_end.isoformat(),
        "current_start": current_start.isoformat(),
        "current_end": current_end.isoformat(),
    }
    return baseline, current, window_bounds


def _metric_probe_versions(
    session: Session,
    endpoint_id: int,
    metric_name: str,
    origin: DataOrigin,
) -> list[ProbeVersion]:
    """Probe versions that recorded this metric for the endpoint/origin."""
    ids = list(
        session.scalars(
            select(ProbeVersion.id)
            .join(Observation, Observation.probe_version_id == ProbeVersion.id)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .join(Score, Score.observation_id == Observation.id)
            .where(
                Observation.endpoint_id == endpoint_id,
                Score.metric_name == metric_name,
                SuiteRun.data_origin == origin,
            )
            .distinct()
        ).all()
    )
    if not ids:
        return []
    return list(session.scalars(select(ProbeVersion).where(ProbeVersion.id.in_(ids))).all())


def _unresolved_defects_for_metric(
    session: Session,
    endpoint_id: int,
    metric_name: str,
    origin: DataOrigin,
    probe_version_id: int | None = None,
) -> int:
    from endpoint_ledger.db.models import ScoreAdjudication

    query = (
        select(ScoreAdjudication.id)
        .join(Score, Score.id == ScoreAdjudication.score_id)
        .join(Observation, Observation.id == Score.observation_id)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            Score.metric_name == metric_name,
            SuiteRun.data_origin == origin,
            ScoreAdjudication.classification.in_(sorted(MEASUREMENT_DEFECT_CLASSES)),
        )
    )
    if probe_version_id is not None:
        query = query.where(Observation.probe_version_id == probe_version_id)
    return len(session.execute(query).all())


def detect_changes(
    session: Session,
    settings: Settings | None = None,
    endpoint_id: int | None = None,
    split_after: datetime | None = None,
    origin: DataOrigin = LIVE_ORIGIN,
    require_qualified_cohort: bool = True,
) -> DetectionResult:
    """Run change detection with measurement-integrity guards.

    Returns events plus refusals. A refusal means the detector deliberately did
    not make a behavioral claim (unqualified cohort, version mismatch, model
    change, contamination, unresolved defects, or insufficient samples).
    """
    settings = settings or get_settings()
    if split_after is None:
        split_after = settings.split_after_datetime or (
            datetime.now(UTC) - timedelta(days=7)
        )

    result = DetectionResult()
    cohort_service = CohortService(session, settings)
    cohort_service.sync_cohorts(origin)
    cohort_service.refresh_qualification_statuses()

    metric_names = sorted(settings_binary_metric_names() | {LATENCY_METRIC})
    endpoint_query = select(Observation.endpoint_id).distinct()
    if endpoint_id is not None:
        endpoint_query = endpoint_query.where(Observation.endpoint_id == endpoint_id)
    endpoint_ids = [row[0] for row in session.execute(endpoint_query).all()]

    # Batch bookkeeping for Benjamini-Hochberg multiple-comparison correction.
    pending: list[tuple[int, str, Comparison, dict[str, Any], dict[str, Any] | None]] = []

    for ep_id in endpoint_ids:
        for metric_name in metric_names:
            if metric_name == LATENCY_METRIC:
                baseline, current, bounds = _load_samples(session, ep_id, metric_name, split_after, origin)
                comparison = compare_latency(
                    metric_name, None, baseline, current,
                    settings.change_detection_min_observations,
                    float(settings.change_detection_min_absolute_change) * 1000,
                    settings.change_detection_alpha,
                )
                if comparison.qualifies:
                    result.events.append(
                        _upsert_event(session, ep_id, comparison, bounds, settings, split_after, origin)
                    )
                continue

            # Behavioral claims are per probe version: a metric may be shared
            # by several probes (e.g. both structured-output probes emit
            # structured_output_task_success).
            versions = _metric_probe_versions(session, ep_id, metric_name, origin)
            if not versions:
                result.refusals.append(
                    DetectionRefusal(
                        ep_id, metric_name,
                        f"no {origin} observations recorded this primary metric "
                        "(legacy probe versions predate composite primary scorers)",
                    )
                )
                continue
            for version in versions:
                refusal = _guard_check(
                    session, settings, cohort_service, ep_id, metric_name,
                    origin, require_qualified_cohort, version,
                )
                if refusal is not None:
                    result.refusals.append(refusal)
                    continue

                if origin == LIVE_ORIGIN:
                    # Live claims use run-level success rates: repeats inside a
                    # SuiteRun are technical replicates, never independent time
                    # samples. Observation-level Fisher is not enough to claim a
                    # confirmed live change.
                    run_level, comparison, bounds = _compare_run_level_for_version(
                        session, settings, cohort_service, ep_id, metric_name, version, split_after, origin
                    )
                    if not comparison.qualifies:
                        if comparison.reason and comparison.reason.startswith("insufficient"):
                            result.refusals.append(
                                DetectionRefusal(ep_id, metric_name, comparison.reason)
                            )
                        continue
                    pending.append((ep_id, metric_name, comparison, bounds, run_level))
                else:
                    baseline, current, bounds = _load_samples(
                        session, ep_id, metric_name, split_after, origin, probe_version_id=version.id
                    )
                    comparison = compare_binary(
                        metric_name, None, baseline, current,
                        settings.change_detection_min_observations,
                        settings.change_detection_min_absolute_change,
                        settings.change_detection_alpha,
                    )
                    if comparison.qualifies:
                        pending.append((ep_id, metric_name, comparison, bounds, None))

    # Multiple-comparison correction across the batch.
    p_values = [c.p_value or 1.0 for _ep, _m, c, _b, _rl in pending]
    q_values = benjamini_hochberg(p_values) if p_values else []
    for (ep_id, metric_name, comparison, bounds, rl), q in zip(
        pending, q_values, strict=True
    ):
        if q >= settings.change_detection_alpha:
            result.refusals.append(
                DetectionRefusal(
                    ep_id, metric_name,
                    f"q-value {q:.4f} >= alpha {settings.change_detection_alpha} after "
                    "Benjamini-Hochberg correction",
                )
            )
            continue
        result.events.append(
            _upsert_event(
                session, ep_id, comparison, bounds, settings, split_after, origin,
                q_value=q, run_level=rl,
            )
        )
    session.commit()
    return result


def _compare_run_level_for_version(
    session: Session,
    settings: Settings,
    cohort_service: CohortService,
    endpoint_id: int,
    metric_name: str,
    version: ProbeVersion,
    split_after: datetime,
    origin: DataOrigin,
) -> tuple[dict[str, Any], Comparison, dict[str, Any]]:
    """Run-level comparison for one live probe version.

    Returns ``(run_level_evidence, comparison, window_bounds)``.
    """
    from endpoint_ledger.db.models import ProviderEndpoint
    from endpoint_ledger.measurement.cohorts import cohort_dimensions, fingerprint_for_dimensions
    from endpoint_ledger.measurement.runlevel import compare_run_level, compute_run_aggregates

    endpoint = session.get(ProviderEndpoint, endpoint_id)
    observations = session.scalars(
        select(Observation)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            Observation.probe_version_id == version.id,
            SuiteRun.data_origin == origin,
            Observation.error_type.is_(None),
        )
        .order_by(Observation.completed_at)
    ).all()
    if not observations:
        empty = Comparison(
            metric_name=metric_name, probe_category=None, kind="run_level",
            baseline_value=None, current_value=None, absolute_change=None,
            relative_change=None, p_value=None, baseline_n=0, current_n=0,
            direction=None, qualifies=False, reason="insufficient evidence: no eligible observations",
        )
        return {}, empty, {}
    if endpoint is None:  # pragma: no cover
        raise RuntimeError("endpoint not found during run-level comparison")
    dimensions = cohort_dimensions(settings, endpoint, version, observations[-1])
    fingerprint = fingerprint_for_dimensions(dimensions)
    aggregates = compute_run_aggregates(
        session, endpoint_id, version.id, metric_name, origin, fingerprint=fingerprint
    )
    def _ts(a) -> datetime:
        return a.completed_at or a.started_at or split_after

    baseline_aggs = [a for a in aggregates if _ts(a) <= split_after]
    current_aggs = [a for a in aggregates if _ts(a) > split_after]

    comparison = compare_run_level(
        baseline_aggs,
        current_aggs,
        min_baseline_runs=settings.change_detection_min_baseline_runs,
        min_current_runs=settings.change_detection_min_current_runs,
        min_current_dates=settings.change_detection_min_current_dates,
        material_effect=settings.change_detection_material_effect,
        alpha=settings.change_detection_alpha,
        n_permutations=settings.change_detection_permutations,
        require_direction_consistency=settings.change_detection_require_direction_consistency,
    )

    wrapped = Comparison(
        metric_name=metric_name,
        probe_category=None,
        kind="run_level",
        baseline_value=comparison.baseline_mean_rate,
        current_value=comparison.current_mean_rate,
        absolute_change=comparison.effect,
        relative_change=(
            comparison.effect / comparison.baseline_mean_rate
            if comparison.effect is not None and comparison.baseline_mean_rate
            else None
        ),
        p_value=comparison.p_value,
        baseline_n=comparison.baseline_runs,
        current_n=comparison.current_runs,
        direction=comparison.direction,
        qualifies=comparison.qualifies,
        reason=comparison.reason,
    )

    def _window(bounds_aggs: list) -> tuple[datetime, datetime]:
        times = [a.completed_at or a.started_at for a in bounds_aggs if (a.completed_at or a.started_at)]
        if not times:
            return split_after, split_after
        return min(times), max(times)

    b_start, b_end = _window(baseline_aggs)
    c_start, c_end = _window(current_aggs)
    bounds = {
        "baseline_start": b_start.isoformat(),
        "baseline_end": b_end.isoformat(),
        "current_start": c_start.isoformat(),
        "current_end": c_end.isoformat(),
    }
    run_level = {
        "method": "run_level_permutation",
        "baseline_runs": comparison.baseline_runs,
        "current_runs": comparison.current_runs,
        "current_utc_dates": comparison.current_utc_dates,
        "baseline_mean_rate": comparison.baseline_mean_rate,
        "current_mean_rate": comparison.current_mean_rate,
        "effect": comparison.effect,
        "direction_consistent": comparison.direction_consistent,
        "min_baseline_runs": settings.change_detection_min_baseline_runs,
        "min_current_runs": settings.change_detection_min_current_runs,
        "min_current_dates": settings.change_detection_min_current_dates,
        "material_effect": settings.change_detection_material_effect,
        "permutations": settings.change_detection_permutations,
        "cohort_fingerprint": fingerprint,
    }
    return run_level, wrapped, bounds


def settings_binary_metric_names() -> set[str]:
    """The primary behavioral metrics eligible for live change claims."""
    from endpoint_ledger.measurement.roles import COMPOSITE_PRIMARY_DEFS

    return set(COMPOSITE_PRIMARY_DEFS)


def _guard_check(
    session: Session,
    settings: Settings,
    cohort_service: CohortService,
    endpoint_id: int,
    metric_name: str,
    origin: DataOrigin,
    require_qualified_cohort: bool,
    version: ProbeVersion,
) -> DetectionRefusal | None:
    """Return a refusal reason, or None when the metric passes the guards."""
    if not version.baseline_eligible:
        return DetectionRefusal(
            endpoint_id, metric_name,
            "probe version is marked baseline-ineligible; behavioral claims are not permitted",
        )

    from endpoint_ledger.db.models import ProviderEndpoint

    endpoint = session.get(ProviderEndpoint, endpoint_id)
    if endpoint is None:  # pragma: no cover
        return DetectionRefusal(endpoint_id, metric_name, "endpoint not found")

    primary = version.primary_metric
    if primary != metric_name:
        return DetectionRefusal(
            endpoint_id, metric_name,
            f"metric {metric_name} is not the primary metric of probe version "
            f"v{version.version_number} (primary is {primary}); behavioral claims use "
            "primary metrics only",
        )

    if _unresolved_defects_for_metric(session, endpoint_id, metric_name, origin, version.id) > 0:
        return DetectionRefusal(
            endpoint_id, metric_name,
            "unresolved measurement defects exist on this metric; claim refused",
        )

    # Cohort compatibility: fingerprint must exist and be qualified.
    observations = session.scalars(
        select(Observation)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            Observation.probe_version_id == version.id,
            SuiteRun.data_origin == origin,
            Observation.error_type.is_(None),
        )
        .order_by(Observation.completed_at)
    ).all()
    if not observations:
        return DetectionRefusal(endpoint_id, metric_name, "no eligible observations")

    reported_models = {o.model_reported for o in observations}
    if len(reported_models) > 1:
        return DetectionRefusal(
            endpoint_id, metric_name,
            "provider-reported model differs across observations "
            f"({sorted(str(m) for m in reported_models)}); observations must not be combined",
        )
    if endpoint is None:  # pragma: no cover
        raise RuntimeError("endpoint not found during run-level comparison")
    dimensions = cohort_dimensions(settings, endpoint, version, observations[-1])
    fingerprint = fingerprint_for_dimensions(dimensions)

    from endpoint_ledger.db.models import BaselineCohort

    cohort = session.scalar(select(BaselineCohort).where(BaselineCohort.fingerprint == fingerprint))
    if cohort is None:
        return DetectionRefusal(
            endpoint_id, metric_name,
            "no baseline cohort exists for this observation fingerprint",
        )
    progress = cohort_service.qualification_progress(cohort)
    if cohort.status == "invalidated":
        return DetectionRefusal(
            endpoint_id, metric_name,
            "baseline cohort is invalidated: " + (cohort.invalidation_reason or "unknown reason"),
        )
    if require_qualified_cohort and not progress["qualified"]:
        unmet = "; ".join(progress["unmet_requirements"])
        return DetectionRefusal(
            endpoint_id, metric_name,
            f"baseline cohort is unqualified (status {cohort.status}): {unmet}",
        )
    if cohort.model_reported and reported_models and cohort.model_reported != reported_models.pop():
        return DetectionRefusal(
            endpoint_id, metric_name,
            f"provider-reported model {reported_models} does not match cohort "
            f"model_reported {cohort.model_reported}; new cohort required",
        )
    return None


def _upsert_event(
    session: Session,
    endpoint_id: int,
    comparison: Comparison,
    bounds: dict[str, Any],
    settings: Settings,
    split_after: datetime,
    origin: DataOrigin,
    q_value: float | None = None,
    run_level: dict[str, Any] | None = None,
) -> ChangeEvent:
    baseline_start = datetime.fromisoformat(bounds["baseline_start"])
    current_end = datetime.fromisoformat(bounds["current_end"])
    p_value = comparison.p_value
    confidence = (1.0 - p_value) if p_value is not None else 0.0

    existing = session.scalar(
        select(ChangeEvent).where(
            ChangeEvent.endpoint_id == endpoint_id,
            ChangeEvent.metric_name == comparison.metric_name,
            ChangeEvent.probe_category == comparison.probe_category,
            ChangeEvent.baseline_start == baseline_start,
            ChangeEvent.current_end == current_end,
            ChangeEvent.status.in_(["provisional", "confirmed"]),
        )
    )
    evidence = {
        "baseline_value": comparison.baseline_value,
        "current_value": comparison.current_value,
        "absolute_change": comparison.absolute_change,
        "relative_change": comparison.relative_change,
        "p_value": p_value,
        "q_value": q_value,
        "baseline_n": comparison.baseline_n,
        "current_n": comparison.current_n,
        "split_after": split_after.isoformat(),
        "thresholds": {
            "min_observations": settings.change_detection_min_observations,
            "min_absolute_change": settings.change_detection_min_absolute_change,
            "alpha": settings.change_detection_alpha,
        },
        "windows": bounds,
        "compatibility": {
            "data_origin": origin,
            "cohort_qualified": True,
            "single_probe_version": True,
            "model_reported_matches": True,
        },
        "run_level": run_level,
    }
    now = datetime.now(UTC)
    explanation = f"{comparison.metric_name} {comparison.kind}: {comparison.reason}"
    if q_value is not None:
        explanation += f" (q={q_value:.4f})"

    if existing is not None:
        existing.metric_name = comparison.metric_name
        existing.direction = comparison.direction or "unknown"
        existing.baseline_start = datetime.fromisoformat(bounds["baseline_start"])
        existing.baseline_end = datetime.fromisoformat(bounds["baseline_end"])
        existing.current_start = datetime.fromisoformat(bounds["current_start"])
        existing.current_end = datetime.fromisoformat(bounds["current_end"])
        existing.baseline_value = comparison.baseline_value or 0.0
        existing.current_value = comparison.current_value or 0.0
        existing.absolute_change = comparison.absolute_change or 0.0
        existing.relative_change = comparison.relative_change
        existing.p_value = p_value
        existing.confidence_level = confidence
        existing.affected_observation_count = comparison.baseline_n + comparison.current_n
        existing.status = "provisional"
        existing.last_confirmed_at = now
        existing.explanation = explanation
        existing.evidence = evidence
        existing.data_origin = origin
        session.flush()
        return existing

    event = ChangeEvent(
        endpoint_id=endpoint_id,
        metric_name=comparison.metric_name,
        probe_category=comparison.probe_category,
        direction=comparison.direction or "unknown",
        baseline_start=datetime.fromisoformat(bounds["baseline_start"]),
        baseline_end=datetime.fromisoformat(bounds["baseline_end"]),
        current_start=datetime.fromisoformat(bounds["current_start"]),
        current_end=datetime.fromisoformat(bounds["current_end"]),
        baseline_value=comparison.baseline_value or 0.0,
        current_value=comparison.current_value or 0.0,
        absolute_change=comparison.absolute_change or 0.0,
        relative_change=comparison.relative_change,
        p_value=p_value,
        confidence_level=confidence,
        affected_observation_count=comparison.baseline_n + comparison.current_n,
        status="provisional",
        last_confirmed_at=now,
        explanation=explanation,
        evidence=evidence,
        data_origin=origin,
    )
    session.add(event)
    session.flush()
    return event
