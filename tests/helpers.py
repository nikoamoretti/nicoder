"""Shared test helpers for building domain objects."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from endpoint_ledger.db.models import Probe, ProbeVersion, ProviderEndpoint
from endpoint_ledger.measurement.roles import primary_metric_for_definition
from endpoint_ledger.probes.loader import compute_definition_hash, load_probes
from endpoint_ledger.probes.schemas import ProbeDefinition

PROBES_DIR = "probes/smoke"


def load_smoke_probes() -> list[ProbeDefinition]:
    return load_probes(PROBES_DIR)


def make_endpoint(
    session: Session,
    *,
    provider: str = "mock",
    display_name: str = "Mock Test Endpoint",
    model_requested: str = "mock:model-1",
    request_defaults: dict[str, Any] | None = None,
    input_price: float | None = None,
    output_price: float | None = None,
) -> ProviderEndpoint:
    endpoint = ProviderEndpoint(
        provider=provider,
        display_name=display_name,
        model_requested=model_requested,
        request_defaults=request_defaults or {},
        input_cost_per_million_tokens=input_price,
        output_cost_per_million_tokens=output_price,
    )
    session.add(endpoint)
    session.flush()
    return endpoint


def sync_probes(session: Session) -> list[ProbeVersion]:
    from endpoint_ledger.probes.loader import sync_probes_to_db

    return sync_probes_to_db(session, load_smoke_probes())


def make_probe_version(
    session: Session,
    definition: ProbeDefinition,
    version_number: int | None = None,
) -> ProbeVersion:
    probe = session.query(Probe).filter(Probe.stable_key == definition.stable_key).one_or_none()
    if probe is None:
        probe = Probe(
            stable_key=definition.stable_key,
            category=definition.category,
            title=definition.title,
            description=definition.description,
        )
        session.add(probe)
        session.flush()
        next_number = version_number or 1
    else:
        latest = session.query(ProbeVersion).filter(ProbeVersion.probe_id == probe.id).first()
        next_number = version_number or ((latest.version_number + 1) if latest else 1)
    version = ProbeVersion(
        probe_id=probe.id,
        version_number=next_number,
        definition_hash=compute_definition_hash(definition),
        prompt_messages=[m.model_dump() for m in definition.messages],
        tools=definition.tools,
        expected_behavior=definition.expected_behavior,
        request_parameters=definition.request_parameters,
        scorers=definition.scorers,
        primary_metric=primary_metric_for_definition(definition.category, definition.expected_behavior),
    )
    session.add(version)
    session.flush()
    return version
