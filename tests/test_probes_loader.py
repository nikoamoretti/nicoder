"""Probe loading, validation, hashing, and immutability tests."""

from __future__ import annotations

import copy

import pytest

from endpoint_ledger.probes.loader import (
    compute_definition_hash,
    load_probes,
    sync_probes_to_db,
    validate_expected_behavior,
)
from tests.helpers import load_smoke_probes, sync_probes


def test_loads_six_smoke_probes():
    probes = load_smoke_probes()
    assert len(probes) == 6
    assert {p.stable_key for p in probes} == {
        "structured_output_customer_object",
        "structured_output_subscription_status",
        "tool_calling_lookup_order",
        "tool_calling_lookup_then_cancel",
        "instruction_adherence_exactly_three_items",
        "instruction_adherence_confirmation_before_destructive",
    }


def test_unknown_scorer_is_rejected(tmp_path, monkeypatch):

    (tmp_path / "bad.yaml").write_text(
        "stable_key: bad_probe\n"
        "version: 1\n"
        "category: structured_output\n"
        "title: Bad\n"
        "messages:\n  - role: user\n    content: hi\n"
        "expected_behavior:\n  json_schema:\n    type: object\n  required_fields: [x]\n  example: {x: 1}\n"
        "scorers: [not_a_real_scorer]\n"
    )
    with pytest.raises(ValueError, match="unknown scorers"):
        load_probes(tmp_path)


def test_structured_output_requires_json_schema():
    definition = next(p for p in load_smoke_probes() if p.category == "structured_output")
    bad = copy.deepcopy(definition)
    bad.expected_behavior.pop("json_schema")
    with pytest.raises(ValueError, match="requires json_schema"):
        validate_expected_behavior(bad)


def test_invalid_json_schema_is_rejected():
    definition = next(p for p in load_smoke_probes() if p.category == "structured_output")
    bad = copy.deepcopy(definition)
    bad.expected_behavior["json_schema"] = {"type": "not-a-type"}
    with pytest.raises(ValueError, match="json_schema is not valid"):
        validate_expected_behavior(bad)


def test_tool_calling_requires_forbidden_tools():
    definition = next(p for p in load_smoke_probes() if p.category == "tool_calling")
    bad = copy.deepcopy(definition)
    bad.expected_behavior.pop("forbidden_tools")
    with pytest.raises(ValueError, match="requires forbidden_tools"):
        validate_expected_behavior(bad)


def test_definition_hash_is_stable_and_sensitive():
    definition = load_smoke_probes()[0]
    h1 = compute_definition_hash(definition)
    h2 = compute_definition_hash(copy.deepcopy(definition))
    assert h1 == h2
    mutated = copy.deepcopy(definition)
    mutated.expected_behavior["example"]["name"] = "Changed Name"
    assert compute_definition_hash(mutated) != h1


def test_sync_is_idempotent(db):
    first = sync_probes(db)
    db.commit()
    second = sync_probes(db)
    db.commit()
    assert len(first) == len(second) == 6
    keys = [v.probe.stable_key for v in first]
    assert keys == [v.probe.stable_key for v in second]
    version_numbers = [v.version_number for v in second]
    assert version_numbers == [1] * 6
    from endpoint_ledger.db.models import ProbeVersion

    count = db.query(ProbeVersion).count()
    assert count == 6


def test_modified_probe_creates_new_version(db):
    sync_probes(db)
    db.commit()

    from endpoint_ledger.db.models import Probe

    probe = db.query(Probe).filter(Probe.stable_key == "structured_output_customer_object").one()
    old_version = probe.latest_version
    assert old_version is not None
    assert old_version.version_number == 1

    definitions = load_smoke_probes()
    mutated = copy.deepcopy(next(d for d in definitions if d.stable_key == probe.stable_key))
    mutated.expected_behavior["example"]["name"] = "Someone Else"

    sync_probes_to_db(db, [mutated])
    db.commit()

    db.refresh(probe)
    assert len(probe.versions) == 2
    assert probe.versions[0].version_number == 1
    assert probe.versions[1].version_number == 2
    assert probe.latest_version.version_number == 2
    # old version untouched
    assert probe.versions[0].definition_hash == old_version.definition_hash


def test_same_hash_after_resync_does_not_create_new_version(db):
    sync_probes(db)
    db.commit()
    sync_probes(db)
    db.commit()
    from endpoint_ledger.db.models import Probe

    for probe in db.query(Probe).all():
        assert len(probe.versions) == 1
