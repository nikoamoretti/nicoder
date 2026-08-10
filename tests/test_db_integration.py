"""Database integration tests: roundtrips, immutability triggers, versioning."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from endpoint_ledger.db.models import (
    ChangeEvent,
    Observation,
    Probe,
    ProbeVersion,
    ProviderEndpoint,
    Score,
    SuiteRun,
)
from tests.helpers import make_endpoint, sync_probes


def test_endpoint_roundtrip(db):
    endpoint = make_endpoint(
        db,
        request_defaults={"mock": {"mode": "stable_success"}},
        input_price=1.0,
        output_price=2.0,
    )
    db.commit()
    fetched = db.get(ProviderEndpoint, endpoint.id)
    assert fetched is not None
    assert fetched.provider == "mock"
    assert fetched.request_defaults["mock"]["mode"] == "stable_success"
    assert fetched.input_cost_per_million_tokens == 1.0


def test_full_observation_scores_roundtrip(db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success"}})
    versions = sync_probes(db)
    run = SuiteRun(
        suite_name="t",
        status="completed",
        total_expected_observations=1,
        completed_observations=1,
        failed_observations=0,
        runner_version="0.1.0",
    )
    db.add(run)
    db.flush()
    observation = Observation(
        suite_run_id=run.id,
        endpoint_id=endpoint.id,
        probe_version_id=versions[0].id,
        repeat_index=1,
        raw_request={"messages": []},
        raw_request_hash="abc",
        raw_response={"choices": []},
        raw_response_hash="def",
        normalized_text='{"a": 1}',
        input_tokens=5,
        output_tokens=5,
    )
    db.add(observation)
    db.flush()
    score = Score(
        observation_id=observation.id,
        scorer_name="json_parse_success",
        scorer_version="1.0.0",
        metric_name="json_parse_success",
        passed=True,
        explanation="ok",
        details={"ok": True},
    )
    db.add(score)
    db.commit()

    fetched = db.get(Observation, observation.id)
    assert fetched is not None
    assert fetched.endpoint.id == endpoint.id
    assert fetched.probe_version.id == versions[0].id
    assert fetched.scores[0].passed is True
    assert fetched.raw_response_hash == "def"


def test_change_event_roundtrip(db):
    endpoint = make_endpoint(db)
    now = datetime.now(UTC)
    event = ChangeEvent(
        endpoint_id=endpoint.id,
        metric_name="json_schema_validity",
        direction="regression",
        baseline_start=now,
        baseline_end=now,
        current_start=now,
        current_end=now,
        baseline_value=1.0,
        current_value=0.0,
        absolute_change=-1.0,
        confidence_level=0.99,
        explanation="boom",
        evidence={"x": 1},
    )
    db.add(event)
    db.commit()
    assert db.get(ChangeEvent, event.id).status == "provisional"


def test_probe_version_immutable_after_observations(db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success"}})
    versions = sync_probes(db)
    version = versions[0]
    run = SuiteRun(
        suite_name="t",
        status="completed",
        total_expected_observations=1,
        completed_observations=1,
        failed_observations=0,
        runner_version="0.1.0",
    )
    db.add(run)
    db.flush()
    db.add(
        Observation(
            suite_run_id=run.id,
            endpoint_id=endpoint.id,
            probe_version_id=version.id,
            repeat_index=1,
            raw_request={},
            raw_request_hash="h",
        )
    )
    db.commit()

    version.definition_hash = "tampered"
    with pytest.raises(SQLAlchemyError):
        db.commit()
    db.rollback()


def test_probe_version_delete_blocked_after_observations(db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success"}})
    versions = sync_probes(db)
    version = versions[0]
    run = SuiteRun(
        suite_name="t",
        status="completed",
        total_expected_observations=1,
        completed_observations=1,
        failed_observations=0,
        runner_version="0.1.0",
    )
    db.add(run)
    db.flush()
    db.add(
        Observation(
            suite_run_id=run.id,
            endpoint_id=endpoint.id,
            probe_version_id=version.id,
            repeat_index=1,
            raw_request={},
            raw_request_hash="h",
        )
    )
    db.commit()
    db.delete(version)
    with pytest.raises(SQLAlchemyError):
        db.commit()
    db.rollback()


def test_probe_version_editable_without_observations(db):
    versions = sync_probes(db)
    version = versions[0]
    original = version.definition_hash
    version.definition_hash = "updated-before-any-observation"
    db.commit()
    assert version.definition_hash == "updated-before-any-observation"
    assert original != version.definition_hash


def test_duplicate_version_number_rejected(db):
    sync_probes(db)
    probe = db.query(Probe).first()
    db.add(
        ProbeVersion(
            probe_id=probe.id,
            version_number=1,  # already exists
            definition_hash="x",
            prompt_messages=[],
            expected_behavior={},
            request_parameters={},
            scorers=[],
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
