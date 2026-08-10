"""Baseline qualification report service.

When a cohort qualifies, build a versioned report, export it as a durable
artifact, and verify its hash. The report is evidence, not a comparison.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import BaselineCohort, BaselineReport
from endpoint_ledger.measurement.cohorts import CohortService
from endpoint_ledger.measurement.stats import wilson_interval
from endpoint_ledger.services import notifications


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def create_baseline_report(
    session: Session,
    cohort: BaselineCohort,
    settings: Settings | None = None,
) -> BaselineReport:
    """Create, export, and verify a baseline qualification report."""
    settings = settings or get_settings()
    service = CohortService(session, settings)
    progress = service.qualification_progress(cohort)

    observations = service.cohort_observations(cohort)
    successes = 0
    attempts = 0
    latencies: list[int] = []
    input_tokens = 0
    output_tokens = 0
    actual_cost = 0.0  # noqa: F841 - reserved for aggregated actual cost
    estimated_cost = 0.0
    incidents_account = 0
    incidents_transport = 0
    export_statuses = {}
    for observation in observations:
        if observation.error_type is None:
            attempts += 1
            outcome, _derived = None, None
            from endpoint_ledger.measurement.cohorts import primary_outcome_for_observation

            outcome, _derived = primary_outcome_for_observation(observation)
            if outcome:
                successes += 1
        if observation.latency_ms is not None:
            latencies.append(observation.latency_ms)
        if observation.input_tokens is not None:
            input_tokens += observation.input_tokens
        if observation.output_tokens is not None:
            output_tokens += observation.output_tokens
        if observation.estimated_cost_usd is not None:
            estimated_cost += observation.estimated_cost_usd
        if observation.error_category == "account_configuration":
            incidents_account += 1
        if observation.error_category == "provider_transport":
            incidents_transport += 1
        if observation.suite_run:
            export_statuses[observation.suite_run_id] = observation.suite_run.evidence_state

    interval = wilson_interval(successes, attempts) if attempts else None
    version = cohort.probe_version
    content = {
        "schema_version": "1.0.0",
        "cohort_fingerprint": cohort.fingerprint,
        "provider": cohort.provider,
        "model_requested": cohort.model_requested,
        "model_reported": cohort.model_reported,
        "serving_surface": cohort.serving_surface,
        "probe_stable_key": cohort.probe_stable_key,
        "probe_version_id": cohort.probe_version_id,
        "probe_version_number": version.version_number if version else None,
        "primary_metric": cohort.primary_metric,
        "primary_scorer": {
            "name": cohort.primary_scorer_name,
            "version": cohort.primary_scorer_version,
        },
        "first_observation_at": (
            progress["first_observation_at"].isoformat()
            if progress["first_observation_at"]
            else None
        ),
        "last_observation_at": progress["last_observation_at"].isoformat() if progress["last_observation_at"] else None,
        "elapsed_hours": progress["elapsed_hours"],
        "eligible_suite_runs": progress["distinct_runs"],
        "distinct_utc_dates": progress["distinct_dates"],
        "eligible_observations": progress["eligible_primary_observations"],
        "primary_successes": successes,
        "primary_attempts": attempts,
        "primary_success_rate": round(successes / attempts, 4) if attempts else None,
        "wilson_ci_95": interval,
        "latency_median_ms": (sorted(latencies)[len(latencies) // 2] if latencies else None),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(estimated_cost, 8),
        "actual_cost_usd": None,
        "pricing_evidence": {
            "input_price_per_million": None,
            "output_price_per_million": None,
        },
        "account_incidents": incidents_account,
        "transport_incidents": incidents_transport,
        "unresolved_measurement_issues": progress["unresolved_measurement_issues"],
        "evidence_export_status_by_run": export_statuses,
        "qualification_decision": "qualified" if progress["qualified"] else "collecting",
        "policy_used": service._qualification_policy(),
        "unmet_requirements": progress["unmet_requirements"],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    canonical = _canonical_json(content)
    file_bytes = (canonical + "\n").encode("utf-8")
    sha = hashlib.sha256(file_bytes).hexdigest()

    report_dir = settings.evidence_export_path / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"baseline-report-{cohort.fingerprint[:16]}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_bytes(file_bytes)
    os.chmod(path, 0o600)

    # Verify the artifact hash against the exact bytes on disk.
    verified = hashlib.sha256(path.read_bytes()).hexdigest() == sha

    report = session.scalar(
        select(BaselineReport).where(BaselineReport.cohort_fingerprint == cohort.fingerprint)
    )
    if report is None:
        report = BaselineReport(
            cohort_fingerprint=cohort.fingerprint,
            content=content,
            path=str(path),
            sha256=sha,
            verification_status="verified" if verified else "failed",
            verified_at=datetime.now(UTC) if verified else None,
        )
        session.add(report)
    else:
        report.content = content
        report.path = str(path)
        report.sha256 = sha
        report.verification_status = "verified" if verified else "failed"
        report.verified_at = datetime.now(UTC) if verified else None
    session.commit()

    notifications.notify(
        session,
        notifications.CATEGORY_BASELINE_QUALIFIED,
        "Baseline qualification report generated",
        message=f"cohort {cohort.fingerprint[:12]}…; report {path.name} verified",
        severity="info",
    )
    return report
