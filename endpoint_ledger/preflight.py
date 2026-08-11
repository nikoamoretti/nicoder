"""Baseline preflight.

Readiness check for baseline collection. Never calls a provider. Reports the
planned matrix, cohort fingerprints, qualification progress, backup and export
state, unresolved issues, and whether the API credential is present — without
displaying it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import Observation, ProviderEndpoint, Score, ScoreAdjudication, SuiteRun
from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES
from endpoint_ledger.measurement.cohorts import CohortService, cohort_dimensions, fingerprint_for_dimensions
from endpoint_ledger.probes.loader import load_probes, sync_probes_to_db
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from endpoint_ledger.sanitize import sanitize_message


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_preflight(
    session: Session,
    settings: Settings | None = None,
    *,
    provider: str,
    endpoint_filter: str,
    suite: str,
    repeats: int,
    cost_ceiling: float | None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    report: dict[str, Any] = {"provider": provider, "ready": False, "refusals": [], "checks": {}}

    from endpoint_ledger.services.credentials import CredentialResolutionError, resolve_credential

    try:
        credential = resolve_credential(session, provider, settings)
        credential_present = True
        report["checks"]["api_credential_present"] = True
        report["credential"] = {
            "source": credential.source,
            "fingerprint": credential.fingerprint,
        }
    except CredentialResolutionError as exc:
        credential_present = False
        report["checks"]["api_credential_present"] = False
        report["checks"]["credential_resolution_reason"] = exc.reason
        report["refusals"].append(sanitize_message(str(exc)))

    endpoints = list(
        session.scalars(
            select(ProviderEndpoint).where(
                ProviderEndpoint.provider == provider,
                ProviderEndpoint.enabled.is_(True),
            )
        ).all()
    )
    matched = [e for e in endpoints if e.model_requested == endpoint_filter or e.display_name == endpoint_filter]
    if not matched:
        report["refusals"].append(f"no enabled endpoint for provider {provider!r} filter {endpoint_filter!r}")
        return report
    endpoint = matched[0]
    report["endpoint"] = {
        "id": endpoint.id,
        "display_name": endpoint.display_name,
        "model_requested": endpoint.model_requested,
    }

    definitions = load_probes(settings.probes_path)
    versions = sync_probes_to_db(session, definitions)
    eligible = [v for v in versions if v.baseline_eligible]
    ineligible = [v for v in versions if not v.baseline_eligible]

    probe_reports: list[dict[str, Any]] = []
    for version in eligible:
        from endpoint_ledger.scoring import registry

        scorer = registry.get(version.primary_metric or "")
        dimensions = cohort_dimensions(settings, endpoint, version)
        probe_reports.append(
            {
                "probe": version.probe.stable_key,
                "probe_version_id": version.id,
                "version_number": version.version_number,
                "baseline_eligible": True,
                "primary_metric": version.primary_metric,
                "primary_scorer": scorer.name if scorer else version.primary_metric,
                "primary_scorer_version": scorer.version if scorer else "legacy-derived",
                "request_parameters_hash": _hash(version.request_parameters or {}),
                "tool_schema_hash": _hash(version.tools) if version.tools else None,
                "cohort_fingerprint": fingerprint_for_dimensions(dimensions),
            }
        )
    for version in ineligible:
        probe_reports.append(
            {
                "probe": version.probe.stable_key,
                "probe_version_id": version.id,
                "version_number": version.version_number,
                "baseline_eligible": False,
                "ineligibility_reason": version.ineligibility_reason,
                "primary_metric": version.primary_metric,
            }
        )
    report["probes"] = probe_reports

    for version in ineligible:
        report["refusals"].append(
            f"probe {version.probe.stable_key} v{version.version_number} is baseline-ineligible"
        )
    missing_primary = [v for v in eligible if not v.primary_metric]
    if missing_primary:
        report["refusals"].append(
            f"{len(missing_primary)} eligible probe version(s) lack a primary metric"
        )

    runner = SuiteRunner(session, settings)
    options = SuiteOptions(
        suite_name=suite, provider=provider, endpoint_filter=endpoint_filter,
        repeats=repeats, eligible_only=True,
    )
    plan = runner.plan(options)
    report["expected_request_count"] = plan.planned_observations
    report["repeats"] = repeats
    estimate = plan.estimate_usd
    report["conservative_max_cost_usd"] = estimate
    if estimate is None:
        report["refusals"].append("cost cannot be bounded (endpoint pricing not configured)")
    elif cost_ceiling is not None and estimate > cost_ceiling:
        report["refusals"].append(
            f"estimated max cost ${estimate:.6f} exceeds ceiling ${cost_ceiling:.6f}"
        )

    cohort_service = CohortService(session, settings)
    try:
        cohort_reports = cohort_service.status_report(provider=provider, origin="live")
    except Exception:  # pragma: no cover - defensive
        cohort_reports = []
    report["baseline_progress"] = [
        {"probe": r["cohort"].probe_stable_key, "status": r["progress"]["status"],
         "eligible_observations": r["progress"]["eligible_primary_observations"],
         "min_primary_observations": r["progress"]["min_primary_observations"],
         "distinct_runs": r["progress"]["distinct_runs"],
         "min_distinct_runs": r["progress"]["min_distinct_runs"],
         "unmet": r["progress"]["unmet_requirements"]}
        for r in cohort_reports
    ]

    from endpoint_ledger.backup import last_verified_backup

    report["most_recent_verified_backup"] = (
        str(last_verified_backup(settings)) if last_verified_backup(settings) else None
    )

    export_dir = settings.evidence_export_path
    export_ok = True
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        probe_file = export_dir / ".write-test"
        probe_file.write_text("ok")
        probe_file.unlink()
    except OSError as exc:  # pragma: no cover
        export_ok = False
        report["refusals"].append(f"evidence export destination unavailable: {exc}")
    report["evidence_export_destination"] = str(export_dir)
    report["evidence_export_destination_ok"] = export_ok

    unresolved = len(
        session.execute(
            select(ScoreAdjudication.id)
            .join(Score, Score.id == ScoreAdjudication.score_id)
            .join(Observation, Observation.id == Score.observation_id)
            .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
            .where(
                SuiteRun.data_origin == "live",
                ScoreAdjudication.classification.in_(sorted(MEASUREMENT_DEFECT_CLASSES)),
            )
        ).all()
    )
    report["unresolved_measurement_issues"] = unresolved
    if unresolved:
        report["refusals"].append(f"{unresolved} unresolved measurement issue(s)")

    live_exists = session.scalar(select(SuiteRun.id).where(SuiteRun.data_origin == "live").limit(1)) is not None
    if live_exists and not report["most_recent_verified_backup"]:
        report["refusals"].append(
            f"no verified backup within {settings.backup_max_age_hours}h while live evidence exists"
        )

    if not credential_present:
        report["refusals"].append("API credential is missing or ambiguous (select an active source)")

    report["ready"] = not report["refusals"]
    return report
