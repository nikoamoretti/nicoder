"""Probe loading, validation, and versioned persistence.

Probes live as version-controlled YAML files. ``load_probes`` parses and
validates them; ``sync_probes_to_db`` persists them idempotently, creating a
new immutable ``ProbeVersion`` whenever a definition changes. Never update an
existing ``ProbeVersion`` row after observations reference it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from endpoint_ledger.db.models import Probe, ProbeVersion
from endpoint_ledger.probes.schemas import ProbeDefinition
from endpoint_ledger.scoring import registry


def load_probes(directory: Path | str) -> list[ProbeDefinition]:
    """Load and validate all probe YAML files in ``directory`` (non-recursive)."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"probes directory not found: {directory}")
    definitions: dict[str, ProbeDefinition] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("."):
            continue
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        definition = ProbeDefinition.model_validate(raw)
        if definition.stable_key in definitions:
            raise ValueError(
                f"duplicate stable_key {definition.stable_key!r} across probe files"
            )
        unknown = registry.unknown(definition.scorers)
        if unknown:
            raise ValueError(
                f"probe {definition.stable_key!r} references unknown scorers: {unknown}"
            )
        validate_expected_behavior(definition)
        definitions[definition.stable_key] = definition
    return sorted(definitions.values(), key=lambda d: d.stable_key)


def validate_expected_behavior(definition: ProbeDefinition) -> None:
    """Validate ``expected_behavior`` shape and JSON Schemas per category."""
    eb: dict[str, Any] = definition.expected_behavior
    category = definition.category

    if category == "structured_output":
        if "json_schema" not in eb:
            raise ValueError(
                f"probe {definition.stable_key!r}: structured_output requires json_schema"
            )
        try:
            jsonschema.Draft202012Validator.check_schema(eb["json_schema"])
        except jsonschema.SchemaError as exc:
            raise ValueError(
                f"probe {definition.stable_key!r}: json_schema is not valid: {exc.message}"
            ) from exc
        if not eb.get("required_fields"):
            raise ValueError(
                f"probe {definition.stable_key!r}: structured_output requires required_fields"
            )
        if "example" not in eb:
            raise ValueError(
                f"probe {definition.stable_key!r}: structured_output requires example"
            )
    elif category == "tool_calling":
        if not eb.get("expected_tool_calls"):
            raise ValueError(
                f"probe {definition.stable_key!r}: tool_calling requires expected_tool_calls"
            )
        if "forbidden_tools" not in eb:
            raise ValueError(
                f"probe {definition.stable_key!r}: tool_calling requires forbidden_tools"
            )
        if "tool_call_sequence" not in eb:
            raise ValueError(
                f"probe {definition.stable_key!r}: tool_calling requires tool_call_sequence"
            )
    elif category == "instruction_adherence":
        has_item_count = "item_count" in eb
        has_contract = "response_contract" in eb
        if not (has_item_count or has_contract):
            raise ValueError(
                f"probe {definition.stable_key!r}: instruction_adherence requires "
                "item_count or response_contract"
            )
        if has_contract:
            try:
                jsonschema.Draft202012Validator.check_schema(eb["response_contract"])
            except jsonschema.SchemaError as exc:
                raise ValueError(
                    f"probe {definition.stable_key!r}: response_contract is not valid: {exc.message}"
                ) from exc
        if has_item_count and has_contract:
            raise ValueError(
                f"probe {definition.stable_key!r}: instruction_adherence must declare "
                "exactly one of item_count or response_contract, not both"
            )
    else:  # pragma: no cover - guarded by Literal type
        raise ValueError(f"unknown probe category: {category}")

    from endpoint_ledger.measurement.roles import primary_metric_for_definition

    primary = primary_metric_for_definition(category, eb)
    if primary not in definition.scorers:
        raise ValueError(
            f"probe {definition.stable_key!r}: primary metric {primary!r} must be listed in scorers"
        )


def compute_definition_hash(definition: ProbeDefinition) -> str:
    """Deterministic SHA-256 of the full serialized definition."""
    payload = json.dumps(definition.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sync_probes_to_db(
    session: Any,
    definitions: list[ProbeDefinition],
) -> list[ProbeVersion]:
    """Idempotently persist probe definitions.

    Re-running with identical definitions is a no-op. A changed definition
    creates a new immutable ``ProbeVersion`` while preserving prior versions.
    """
    from sqlalchemy import select

    synced: list[ProbeVersion] = []
    for definition in definitions:
        definition_hash = compute_definition_hash(definition)
        probe = session.scalar(
            select(Probe).where(Probe.stable_key == definition.stable_key)
        )
        if probe is None:
            probe = Probe(
                stable_key=definition.stable_key,
                category=definition.category,
                title=definition.title,
                description=definition.description,
                enabled=True,
            )
            session.add(probe)
            session.flush()
            version_number = 1
            version = _make_version(probe, definition, definition_hash, version_number)
            session.add(version)
            session.flush()
            synced.append(version)
            continue

        probe.category = definition.category
        probe.title = definition.title
        probe.description = definition.description
        latest = session.scalar(
            select(ProbeVersion)
            .where(ProbeVersion.probe_id == probe.id)
            .order_by(ProbeVersion.version_number.desc())
        )
        if latest is not None and latest.definition_hash == definition_hash:
            synced.append(latest)
            continue
        next_number = (latest.version_number + 1) if latest else 1
        version = _make_version(probe, definition, definition_hash, next_number)
        session.add(version)
        session.flush()
        synced.append(version)
    session.flush()
    return synced


def _make_version(
    probe: Probe,
    definition: ProbeDefinition,
    definition_hash: str,
    version_number: int,
) -> ProbeVersion:
    from endpoint_ledger.measurement.roles import primary_metric_for_definition

    return ProbeVersion(
        probe_id=probe.id,
        version_number=version_number,
        definition_hash=definition_hash,
        prompt_messages=[m.model_dump() for m in definition.messages],
        tools=definition.tools,
        expected_behavior=definition.expected_behavior,
        request_parameters=definition.request_parameters,
        scorers=definition.scorers,
        primary_metric=primary_metric_for_definition(definition.category, definition.expected_behavior),
    )
