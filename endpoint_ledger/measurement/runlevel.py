"""Run-level aggregation and comparison.

The repeats inside one SuiteRun are technical replicates, not independent time
samples. For live change claims we aggregate per
(endpoint, cohort, probe version, primary metric, SuiteRun) and compare run
success rates with a deterministic permutation test. A single SuiteRun can
never confirm a change, regardless of how many repeats it contains.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.db.models import Observation, SuiteRun
from endpoint_ledger.measurement.cohorts import primary_outcome_for_observation

PERMUTATION_SEED = 20260810


@dataclass
class RunAggregate:
    suite_run_id: int
    started_at: datetime | None
    completed_at: datetime | None
    utc_date: date | None
    successes: int
    attempts: int
    rate: float
    model_reported: str | None
    cohort_fingerprint: str | None


def compute_run_aggregates(
    session: Session,
    endpoint_id: int,
    probe_version_id: int,
    primary_metric: str,
    origin: str,
    fingerprint: str | None = None,
) -> list[RunAggregate]:
    """One aggregate per SuiteRun for eligible observations."""
    observations = session.scalars(
        select(Observation)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(
            Observation.endpoint_id == endpoint_id,
            Observation.probe_version_id == probe_version_id,
            SuiteRun.data_origin == origin,
            Observation.error_type.is_(None),
        )
        .order_by(Observation.completed_at)
    ).all()

    by_run: dict[int, list[Observation]] = {}
    for observation in observations:
        by_run.setdefault(observation.suite_run_id, []).append(observation)

    aggregates: list[RunAggregate] = []
    for run_id, group in sorted(by_run.items()):
        run = session.get(SuiteRun, run_id)
        if run is None:  # pragma: no cover
            continue
        outcomes = [primary_outcome_for_observation(o)[0] for o in group]
        outcomes = [o for o in outcomes if o is not None]
        if not outcomes:
            continue
        reported = {o.model_reported for o in group if o.model_reported}
        completed = run.completed_at or run.started_at
        aggregates.append(
            RunAggregate(
                suite_run_id=run_id,
                started_at=run.started_at,
                completed_at=completed,
                utc_date=completed.date() if completed else None,
                successes=sum(1 for o in outcomes if o),
                attempts=len(outcomes),
                rate=sum(1 for o in outcomes if o) / len(outcomes),
                model_reported=reported.pop() if len(reported) == 1 else None,
                cohort_fingerprint=fingerprint,
            )
        )
    return aggregates


@dataclass
class RunLevelComparison:
    baseline_runs: int
    current_runs: int
    current_utc_dates: int
    baseline_mean_rate: float | None
    current_mean_rate: float | None
    effect: float | None
    p_value: float | None
    direction: str | None
    direction_consistent: bool | None
    qualifies: bool
    reason: str


def _permutation_p(
    baseline_rates: list[float],
    current_rates: list[float],
    n_permutations: int,
    rng: random.Random,
) -> float:
    """Two-sided permutation test on the difference of run-level means."""
    observed = sum(current_rates) / len(current_rates) - sum(baseline_rates) / len(baseline_rates)
    all_rates = baseline_rates + current_rates
    n_b = len(baseline_rates)
    extremes = 0
    for _ in range(n_permutations):
        rng.shuffle(all_rates)
        perm_b = all_rates[:n_b]
        perm_c = all_rates[n_b:]
        stat = sum(perm_c) / len(perm_c) - sum(perm_b) / len(perm_b)
        if abs(stat) >= abs(observed):
            extremes += 1
    return (extremes + 1) / (n_permutations + 1)


def compare_run_level(
    baseline: list[RunAggregate],
    current: list[RunAggregate],
    *,
    min_baseline_runs: int,
    min_current_runs: int,
    min_current_dates: int,
    material_effect: float,
    alpha: float,
    n_permutations: int,
    require_direction_consistency: bool,
) -> RunLevelComparison:
    """Compare two periods using run-level success rates.

    Returns an explicit ``reason`` when sample requirements are unmet; the
    caller must surface ``insufficient_evidence`` rather than a confidence
    claim.
    """
    if len(baseline) < min_baseline_runs:
        return RunLevelComparison(
            baseline_runs=len(baseline), current_runs=len(current),
            current_utc_dates=len({a.utc_date for a in current if a.utc_date}),
            baseline_mean_rate=None, current_mean_rate=None, effect=None,
            p_value=None, direction=None, direction_consistent=None, qualifies=False,
            reason=f"insufficient baseline runs ({len(baseline)} of {min_baseline_runs} required)",
        )
    if len(current) < min_current_runs:
        return RunLevelComparison(
            baseline_runs=len(baseline), current_runs=len(current),
            current_utc_dates=len({a.utc_date for a in current if a.utc_date}),
            baseline_mean_rate=None, current_mean_rate=None, effect=None,
            p_value=None, direction=None, direction_consistent=None, qualifies=False,
            reason=f"insufficient current runs ({len(current)} of {min_current_runs} required)",
        )
    current_dates = {a.utc_date for a in current if a.utc_date}
    if len(current_dates) < min_current_dates:
        return RunLevelComparison(
            baseline_runs=len(baseline), current_runs=len(current),
            current_utc_dates=len(current_dates),
            baseline_mean_rate=None, current_mean_rate=None, effect=None,
            p_value=None, direction=None, direction_consistent=None, qualifies=False,
            reason=f"current observations span only {len(current_dates)} UTC date(s); "
            f"{min_current_dates} required",
        )

    baseline_mean = sum(a.rate for a in baseline) / len(baseline)
    current_mean = sum(a.rate for a in current) / len(current)
    effect = current_mean - baseline_mean
    direction = "regression" if effect < 0 else "improvement"
    rng = random.Random(PERMUTATION_SEED)
    p_value = _permutation_p(
        [a.rate for a in baseline], [a.rate for a in current], n_permutations, rng
    )

    direction_consistent = True
    if require_direction_consistency:
        if direction == "regression":
            direction_consistent = all(a.rate < baseline_mean for a in current)
        else:
            direction_consistent = all(a.rate > baseline_mean for a in current)

    missing = []
    if abs(effect) < material_effect:
        missing.append(f"effect {abs(effect):.3f} below material threshold {material_effect}")
    if p_value >= alpha:
        missing.append(f"p-value {p_value:.4f} >= alpha {alpha}")
    if not direction_consistent:
        missing.append("change direction not consistent across current runs")
    qualifies = not missing

    return RunLevelComparison(
        baseline_runs=len(baseline), current_runs=len(current),
        current_utc_dates=len(current_dates),
        baseline_mean_rate=round(baseline_mean, 4), current_mean_rate=round(current_mean, 4),
        effect=round(effect, 4), p_value=p_value, direction=direction,
        direction_consistent=direction_consistent, qualifies=qualifies,
        reason="; ".join(missing) if missing else "run-level material change detected",
    )
