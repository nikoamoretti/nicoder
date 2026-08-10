"""Baseline cohorts.

Observations may only be compared when every cohort dimension matches. A
deterministic fingerprint identifies the cohort; qualification requires
calendar coverage, distinct runs, primary-observation counts, distinct dates,
and no unresolved measurement defects. A change in the provider-reported model
for the same requested alias starts a new cohort and a model-resolution event.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import (
    BaselineCohort,
    ModelResolutionEvent,
    Observation,
    ProbeVersion,
    ProviderEndpoint,
    SuiteRun,
)
from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES
from endpoint_ledger.measurement.origins import LIVE_ORIGIN, DataOrigin
from endpoint_ledger.measurement.roles import COMPOSITE_PRIMARY_DEFS

COHORT_STATUS_COLLECTING = "collecting"
COHORT_STATUS_QUALIFIED = "qualified"
COHORT_STATUS_INVALIDATED = "invalidated"
COHORT_STATUS_ARCHIVED = "archived"

COHORT_STATUSES = (
    COHORT_STATUS_COLLECTING,
    COHORT_STATUS_QUALIFIED,
    COHORT_STATUS_INVALIDATED,
    COHORT_STATUS_ARCHIVED,
)


def cohort_fingerprint(
    *,
    provider: str,
    model_requested: str,
    model_reported: str | None,
    serving_surface: str,
    endpoint_config: dict[str, Any],
    probe_stable_key: str,
    probe_version_id: int,
    primary_metric: str,
    primary_scorer_name: str,
    primary_scorer_version: str,
    request_parameters: dict[str, Any],
    tool_schema: Any,
    runner_compat_version: str,
    data_origin: DataOrigin,
) -> str:
    """Deterministic fingerprint of all comparison-critical dimensions."""
    payload = {
        "provider": provider,
        "model_requested": model_requested,
        "model_reported": model_reported,
        "serving_surface": serving_surface,
        "endpoint_config": endpoint_config,
        "probe_stable_key": probe_stable_key,
        "probe_version_id": probe_version_id,
        "primary_metric": primary_metric,
        "primary_scorer": {"name": primary_scorer_name, "version": primary_scorer_version},
        "request_parameters": request_parameters,
        "tool_schema": tool_schema,
        "runner_compat_version": runner_compat_version,
        "data_origin": data_origin,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolved_serving_surface(settings: Settings, endpoint: ProviderEndpoint) -> str:
    defaults = endpoint.request_defaults or {}
    return str(defaults.get("serving_surface") or settings.serving_surface)


def primary_scorer_identity(probe_version: ProbeVersion) -> tuple[str, str]:
    """Scorer name+version for the primary metric of a probe version."""
    from endpoint_ledger.scoring import registry

    scorer = registry.get(probe_version.primary_metric or "")
    if scorer is not None:
        return scorer.name, scorer.version
    return probe_version.primary_metric or "unknown", "legacy-derived"


def primary_outcome_for_observation(observation: Observation) -> tuple[bool | None, bool]:
    """Primary outcome for an observation.

    Returns ``(passed, derived)``. When the probe version recorded an explicit
    primary score row that is returned directly. Legacy observations (which
    predate explicit primary scores) derive the outcome from their stored
    diagnostic scores using the same deterministic combination rules.
    """
    primary_metric = observation.probe_version.primary_metric
    if not primary_metric:
        return None, False

    explicit = next((sc for sc in observation.scores if sc.metric_name == primary_metric), None)
    if explicit is not None:
        return explicit.passed, False

    sub_metrics = COMPOSITE_PRIMARY_DEFS.get(primary_metric)
    if sub_metrics is None:
        return None, False
    by_metric = {sc.metric_name: sc for sc in observation.scores}
    values: list[bool] = []
    for metric in sub_metrics:
        score = by_metric.get(metric)
        if score is None or score.passed is None:
            return None, True
        values.append(bool(score.passed))
    return all(values), True


def observation_baseline_eligible(observation: Observation) -> tuple[bool, str]:
    """Whether an observation may enter a behavioral baseline cohort."""
    suite = observation.suite_run
    if suite is not None and suite.data_origin != LIVE_ORIGIN:
        return False, f"data origin is {suite.data_origin}, not live"
    if observation.error_type is not None:
        return False, "observation recorded a provider error"
    version = observation.probe_version
    if not version.baseline_eligible:
        return False, version.ineligibility_reason or "probe version marked baseline-ineligible"
    passed, _derived = primary_outcome_for_observation(observation)
    if passed is None:
        return False, "no primary outcome could be determined for the observation"
    for score in observation.scores:
        if score.metric_name == version.primary_metric:
            for adjudication in score.adjudications:
                if adjudication.classification in MEASUREMENT_DEFECT_CLASSES:
                    return False, f"unresolved measurement defect on primary score ({adjudication.classification})"
    return True, ""


def cohort_dimensions(
    settings: Settings,
    endpoint: ProviderEndpoint,
    probe_version: ProbeVersion,
    observation: Observation | None = None,
) -> dict[str, Any]:
    """All comparison-critical dimensions for a probe version x endpoint."""
    reported = observation.model_reported if observation is not None else None
    primary_name, primary_version = primary_scorer_identity(probe_version)
    request_parameters = dict(probe_version.request_parameters or {})
    tool_schema: Any = probe_version.tools
    return {
        "provider": endpoint.provider,
        "model_requested": endpoint.model_requested,
        "model_reported": reported,
        "serving_surface": _resolved_serving_surface(settings, endpoint),
        "endpoint_config": dict(endpoint.request_defaults or {}),
        "probe_stable_key": probe_version.probe.stable_key,
        "probe_version_id": probe_version.id,
        "primary_metric": probe_version.primary_metric or "",
        "primary_scorer_name": primary_name,
        "primary_scorer_version": primary_version,
        "request_parameters": request_parameters,
        "tool_schema": tool_schema,
        "runner_compat_version": settings.runner_compat_version,
        "data_origin": (observation.suite_run.data_origin if observation and observation.suite_run else LIVE_ORIGIN),
    }


def fingerprint_for_dimensions(dimensions: dict[str, Any]) -> str:
    return cohort_fingerprint(
        provider=dimensions["provider"],
        model_requested=dimensions["model_requested"],
        model_reported=dimensions["model_reported"],
        serving_surface=dimensions["serving_surface"],
        endpoint_config=dimensions["endpoint_config"],
        probe_stable_key=dimensions["probe_stable_key"],
        probe_version_id=dimensions["probe_version_id"],
        primary_metric=dimensions["primary_metric"],
        primary_scorer_name=dimensions["primary_scorer_name"],
        primary_scorer_version=dimensions["primary_scorer_version"],
        request_parameters=dimensions["request_parameters"],
        tool_schema=dimensions["tool_schema"],
        runner_compat_version=dimensions["runner_compat_version"],
        data_origin=dimensions["data_origin"],
    )


class CohortService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # --------------------------------------------------------------- sync

    def sync_cohorts(self, origin: DataOrigin = LIVE_ORIGIN) -> list[BaselineCohort]:
        """Upsert cohorts from eligible observations of the given origin."""
        observations = self._eligible_observations(origin)
        created: list[BaselineCohort] = []
        for observation in observations:
            endpoint = observation.endpoint
            version = observation.probe_version
            dimensions = cohort_dimensions(self.settings, endpoint, version, observation)
            fingerprint = fingerprint_for_dimensions(dimensions)
            cohort = self.session.scalar(
                select(BaselineCohort).where(BaselineCohort.fingerprint == fingerprint)
            )
            if cohort is None:
                cohort = BaselineCohort(
                    fingerprint=fingerprint,
                    provider=dimensions["provider"],
                    model_requested=dimensions["model_requested"],
                    model_reported=dimensions["model_reported"],
                    serving_surface=dimensions["serving_surface"],
                    endpoint_id=endpoint.id,
                    probe_stable_key=dimensions["probe_stable_key"],
                    probe_version_id=dimensions["probe_version_id"],
                    primary_metric=dimensions["primary_metric"],
                    primary_scorer_name=dimensions["primary_scorer_name"],
                    primary_scorer_version=dimensions["primary_scorer_version"],
                    runner_version=dimensions["runner_compat_version"],
                    data_origin=dimensions["data_origin"],
                    status=COHORT_STATUS_COLLECTING,
                )
                self.session.add(cohort)
                created.append(cohort)
            else:
                cohort.model_reported = dimensions["model_reported"]
        self.session.flush()
        self._create_model_resolution_events(origin)
        self.session.commit()
        return created

    def _eligible_observations(self, origin: DataOrigin) -> list[Observation]:
        return list(
            self.session.scalars(
                select(Observation)
                .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
                .where(
                    SuiteRun.data_origin == origin,
                    Observation.error_type.is_(None),
                    Observation.completed_at.is_not(None),
                )
                .order_by(Observation.completed_at)
            ).all()
        )

    def _create_model_resolution_events(self, origin: DataOrigin) -> None:
        """Track when one requested model alias returns a new reported model."""
        rows = self.session.execute(
            select(
                ProviderEndpoint.model_requested,
                Observation.model_reported,
                Observation.completed_at,
            )
            .join(Observation, Observation.endpoint_id == ProviderEndpoint.id)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                SuiteRun.data_origin == origin,
                Observation.model_reported.is_not(None),
            )
            .order_by(Observation.completed_at)
        ).all()
        by_alias: dict[str, list[tuple[str, datetime | None]]] = {}
        for model_requested, model_reported, completed_at in rows:
            by_alias.setdefault(model_requested, []).append((model_reported, completed_at))

        for model_requested, reports in by_alias.items():
            seen: list[str] = []
            for reported, completed_at in reports:
                if reported not in seen:
                    seen.append(reported)
                    if len(seen) < 2:
                        continue
                    existing = self.session.scalar(
                        select(ModelResolutionEvent).where(
                            ModelResolutionEvent.requested_model == model_requested,
                            ModelResolutionEvent.new_model_reported == reported,
                        )
                    )
                    if existing is None:
                        self.session.add(
                            ModelResolutionEvent(
                                requested_model=model_requested,
                                old_model_reported=seen[0],
                                new_model_reported=reported,
                                first_observed_at=completed_at or datetime.now(UTC),
                                event_type="model_alias_routing",
                                notes=(
                                    "exact lifecycle/routing event, not a statistically "
                                    "inferred behavioral change"
                                ),
                            )
                        )

    # ------------------------------------------------------- qualification

    def cohort_observations(self, cohort: BaselineCohort) -> list[Observation]:
        rows = self.session.execute(
            select(Observation)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                SuiteRun.data_origin == cohort.data_origin,
                Observation.endpoint_id == cohort.endpoint_id,
                Observation.probe_version_id == cohort.probe_version_id,
                Observation.error_type.is_(None),
            )
        ).scalars().all()
        return [o for o in rows if observation_baseline_eligible(o)[0]]

    def qualification_progress(self, cohort: BaselineCohort) -> dict[str, Any]:
        """Progress toward qualification for one cohort.

        Invalidated cohorts are permanently ineligible.
        """
        if cohort.status == COHORT_STATUS_INVALIDATED:
            return {
                "cohort_id": cohort.id,
                "status": COHORT_STATUS_INVALIDATED,
                "qualified": False,
                "calendar_days": 0,
                "min_calendar_days": self.settings.baseline_min_calendar_days,
                "distinct_runs": 0,
                "min_distinct_runs": self.settings.baseline_min_distinct_runs,
                "eligible_primary_observations": 0,
                "min_primary_observations": self.settings.baseline_min_primary_observations,
                "distinct_dates": 0,
                "min_distinct_dates": self.settings.baseline_min_distinct_dates,
                "unresolved_measurement_issues": 0,
                "first_observation_at": None,
                "last_observation_at": None,
                "unmet_requirements": [
                    "cohort invalidated: " + (cohort.invalidation_reason or "unknown reason")
                ],
            }
        observations = self.cohort_observations(cohort)
        dates = sorted({o.completed_at.date() for o in observations if o.completed_at})
        runs = {o.suite_run_id for o in observations}
        primary_count = len(observations)
        calendar_days = (dates[-1] - dates[0]).days + 1 if len(dates) >= 2 else (1 if dates else 0)
        completed_times = sorted(o.completed_at for o in observations if o.completed_at)
        elapsed_hours = (
            (completed_times[-1] - completed_times[0]).total_seconds() / 3600.0
            if len(completed_times) >= 2
            else 0.0
        )

        unresolved = 0
        for observation in observations:
            for score in observation.scores:
                if score.metric_name == cohort.primary_metric:
                    for adjudication in score.adjudications:
                        if adjudication.classification in MEASUREMENT_DEFECT_CLASSES:
                            unresolved += 1

        policy = self._qualification_policy()
        unmet = []
        if calendar_days < policy["min_calendar_days"]:
            unmet.append(f"{calendar_days} of {policy['min_calendar_days']} calendar days")
        # Binding requirement: at least 168 elapsed hours between the first and
        # last eligible observation (not merely seven displayed dates).
        if elapsed_hours < policy["min_elapsed_hours"]:
            unmet.append(f"{elapsed_hours:.1f} of {policy['min_elapsed_hours']:.0f} elapsed hours")
        if len(runs) < policy["min_distinct_runs"]:
            unmet.append(f"{len(runs)} of {policy['min_distinct_runs']} distinct runs")
        if primary_count < policy["min_primary_observations"]:
            unmet.append(f"{primary_count} of {policy['min_primary_observations']} primary observations")
        if len(dates) < policy["min_distinct_dates"]:
            unmet.append(f"{len(dates)} of {policy['min_distinct_dates']} distinct dates")
        if unresolved > 0:
            unmet.append(f"{unresolved} unresolved measurement defect(s)")
        if not cohort.probe_version.baseline_eligible:
            unmet.append("probe version marked baseline-ineligible")

        qualified = not unmet
        return {
            "cohort_id": cohort.id,
            "status": COHORT_STATUS_QUALIFIED if qualified else COHORT_STATUS_COLLECTING,
            "qualified": qualified,
            "calendar_days": calendar_days,
            "min_calendar_days": policy["min_calendar_days"],
            "elapsed_hours": round(elapsed_hours, 1),
            "min_elapsed_hours": policy["min_elapsed_hours"],
            "distinct_runs": len(runs),
            "min_distinct_runs": policy["min_distinct_runs"],
            "eligible_primary_observations": primary_count,
            "min_primary_observations": policy["min_primary_observations"],
            "distinct_dates": len(dates),
            "min_distinct_dates": policy["min_distinct_dates"],
            "unresolved_measurement_issues": unresolved,
            "first_observation_at": min(dates) if dates else None,
            "last_observation_at": max(dates) if dates else None,
            "unmet_requirements": unmet,
        }

    def _qualification_policy(self) -> dict[str, Any]:
        return {
            "min_calendar_days": self.settings.baseline_min_calendar_days,
            "min_elapsed_hours": self.settings.baseline_min_elapsed_hours,
            "min_distinct_runs": self.settings.baseline_min_distinct_runs,
            "min_primary_observations": self.settings.baseline_min_primary_observations,
            "min_distinct_dates": self.settings.baseline_min_distinct_dates,
        }

    def refresh_qualification_statuses(self) -> None:
        """Recompute cohort statuses from current progress.

        Live cohorts that have lost all eligible observations (e.g. the
        2026-08-09 data-loss incident) are invalidated and hidden from the
        default view; they can never qualify or participate in change detection.
        """
        for cohort in self.session.scalars(select(BaselineCohort)).all():
            progress = self.qualification_progress(cohort)
            if cohort.status == COHORT_STATUS_ARCHIVED or cohort.status == COHORT_STATUS_INVALIDATED:
                continue
            if (
                cohort.data_origin == LIVE_ORIGIN
                and cohort.status == COHORT_STATUS_COLLECTING
                and progress["eligible_primary_observations"] == 0
            ):
                cohort.status = COHORT_STATUS_INVALIDATED
                cohort.invalidation_reason = "no eligible live observations"
                continue
            if progress["status"] == COHORT_STATUS_QUALIFIED and cohort.status != COHORT_STATUS_QUALIFIED:
                cohort.status = COHORT_STATUS_QUALIFIED
        self.session.commit()

    def status_report(
        self,
        provider: str | None = None,
        origin: DataOrigin = LIVE_ORIGIN,
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        self.sync_cohorts(origin)
        self.refresh_qualification_statuses()
        query = select(BaselineCohort).where(BaselineCohort.data_origin == origin)
        if not include_invalidated:
            query = query.where(BaselineCohort.status != COHORT_STATUS_INVALIDATED)
        if provider:
            query = query.where(BaselineCohort.provider == provider)
        reports = []
        for cohort in self.session.scalars(query.order_by(BaselineCohort.id)).all():
            reports.append({"cohort": cohort, "progress": self.qualification_progress(cohort)})
        return reports


def default_date_span(start: date | None, end: date | None) -> int:
    if not start or not end:
        return 0
    return (end - start).days + 1
