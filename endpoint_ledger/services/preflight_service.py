"""Preflight service: wraps the read-only preflight and persists results.

Preflight results carry a configuration hash; a prior result becomes stale
when any relevant configuration changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import PreflightRecord
from endpoint_ledger.preflight import run_preflight
from endpoint_ledger.services.credentials import record_preflight_binding


def configuration_hash(report: dict[str, Any]) -> str:
    """Deterministic hash of the preflight-relevant configuration."""
    payload = {
        "endpoint": report.get("endpoint"),
        "probes": [
            {
                "probe": p.get("probe"),
                "probe_version_id": p.get("probe_version_id"),
                "cohort_fingerprint": p.get("cohort_fingerprint"),
            }
            for p in report.get("probes", [])
        ],
        "expected_request_count": report.get("expected_request_count"),
        "repeats": report.get("repeats"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def perform_preflight(
    session: Session,
    settings: Settings | None = None,
    *,
    provider: str,
    endpoint_filter: str,
    suite: str,
    repeats: int,
    cost_ceiling: float | None,
) -> dict[str, Any]:
    """Run the read-only preflight and persist a PreflightRecord."""
    settings = settings or get_settings()
    report = run_preflight(
        session, settings,
        provider=provider, endpoint_filter=endpoint_filter,
        suite=suite, repeats=repeats, cost_ceiling=cost_ceiling,
    )
    record = PreflightRecord(
        provider=provider,
        configuration_hash=configuration_hash(report),
        ready=bool(report.get("ready")),
        blocking_reasons=list(report.get("refusals") or []),
        report=report,
    )
    record_preflight_binding(session, provider, record, settings)
    session.add(record)
    session.commit()
    report["configuration_hash"] = record.configuration_hash
    report["preflight_record_id"] = record.id
    return report


def latest_preflight(session: Session, provider: str) -> PreflightRecord | None:
    return session.scalar(
        select(PreflightRecord)
        .where(PreflightRecord.provider == provider)
        .order_by(PreflightRecord.created_at.desc())
    )


def preflight_is_stale(session: Session, settings: Settings, provider: str, current_config_hash: str | None) -> bool:
    """A prior preflight is stale when the configuration hash changed."""
    record = latest_preflight(session, provider)
    if record is None:
        return True
    return current_config_hash is not None and record.configuration_hash != current_config_hash
