"""Idempotent seeding of endpoint configurations and probe definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import ProbeVersion, ProviderEndpoint
from endpoint_ledger.probes.loader import load_probes, sync_probes_to_db


def seed_endpoints(session: Session, settings: Settings | None = None) -> list[ProviderEndpoint]:
    """Upsert the built-in mock + openai endpoints, plus any local endpoint
    configuration files under ``local_endpoints/`` if present."""
    settings = settings or get_settings()

    endpoints: list[dict[str, Any]] = [
        {
            "provider": "mock",
            "display_name": "Mock Provider",
            "model_requested": "mock:model-1",
            "request_defaults": {"mock": {"mode": "stable_success", "seed": 1}},
        },
        {
            "provider": "openai",
            "display_name": "OpenAI",
            "model_requested": settings.openai_model,
            "request_defaults": {},
        },
        {
            # Disabled pinned-snapshot endpoint for a later
            # alias-versus-pinned-snapshot comparison. Never invoked
            # automatically; enable only with an explicit request.
            "provider": "openai",
            "display_name": "OpenAI gpt-4o-mini-2024-07-18 (pinned, disabled)",
            "model_requested": "gpt-4o-mini-2024-07-18",
            "request_defaults": {},
            "enabled": False,
        },
    ]

    local_dir = Path(settings.probes_path).parent.parent / "local_endpoints"
    if local_dir.is_dir():
        for path in sorted(local_dir.glob("*.yaml")):
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            endpoints.append(_validate_local_endpoint(raw, path))

    created: list[ProviderEndpoint] = []
    for spec in endpoints:
        endpoint = session.scalar(
            select(ProviderEndpoint).where(
                ProviderEndpoint.provider == spec["provider"],
                ProviderEndpoint.display_name == spec["display_name"],
            )
        )
        if endpoint is None:
            endpoint = ProviderEndpoint(provider=spec["provider"], display_name=spec["display_name"])
            session.add(endpoint)
        endpoint.model_requested = str(spec["model_requested"])
        endpoint.request_defaults = dict(spec.get("request_defaults") or {})
        endpoint.enabled = bool(spec.get("enabled", True))
        endpoint.input_cost_per_million_tokens = spec.get("input_cost_per_million_tokens")
        endpoint.output_cost_per_million_tokens = spec.get("output_cost_per_million_tokens")
        created.append(endpoint)
    session.flush()
    return created


def seed_probes(session: Session, settings: Settings | None = None) -> list[ProbeVersion]:
    settings = settings or get_settings()
    definitions = load_probes(settings.probes_path)
    return sync_probes_to_db(session, definitions)


def seed_all(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    endpoints = seed_endpoints(session, settings)
    versions = seed_probes(session, settings)
    session.commit()
    return {"endpoints": endpoints, "probe_versions": versions}


def _validate_local_endpoint(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    required = {"provider", "display_name", "model_requested"}
    missing = required - set(raw)
    if missing:
        raise ValueError(f"local endpoint {path} missing fields: {sorted(missing)}")
    return raw
