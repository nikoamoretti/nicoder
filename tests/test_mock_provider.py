"""MockProvider behavior tests: deterministic scenario modes."""

from __future__ import annotations

import json

import pytest

from endpoint_ledger.providers.mock import MOCK_MODES, MockProvider
from tests.helpers import load_smoke_probes, make_endpoint, make_probe_version


@pytest.fixture()
def provider():
    return MockProvider(latency_base_ms=100, latency_jitter_ms=0)


def _probe_for(session, stable_key):
    definition = next(d for d in load_smoke_probes() if d.stable_key == stable_key)
    return make_probe_version(session, definition)


def test_stable_success_structured_output(provider, db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    pv = _probe_for(db, "structured_output_customer_object")
    response = provider.invoke(endpoint, pv, 1)
    assert response.error is None
    assert response.http_status == 200
    parsed = json.loads(response.text)
    assert parsed == pv.expected_behavior["example"]
    assert response.usage.input_tokens is not None
    assert response.model_reported == "mock:model-1"


def test_stable_success_tool_calling(provider, db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    pv = _probe_for(db, "tool_calling_lookup_order")
    response = provider.invoke(endpoint, pv, 1)
    assert response.error is None
    assert [c.name for c in response.tool_calls] == ["lookup_order"]
    assert response.tool_calls[0].arguments == {"order_id": "ORD-8821"}


def test_stable_failure(provider, db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_failure", "seed": 1}})
    pv = _probe_for(db, "structured_output_customer_object")
    response = provider.invoke(endpoint, pv, 1)
    assert response.error is not None
    assert response.error.error_type == "server_error"
    assert response.error.http_status == 500
    assert response.text is None


def test_structured_output_regression_violates_schema(provider, db):
    endpoint = make_endpoint(
        db, request_defaults={"mock": {"mode": "structured_output_regression", "seed": 1}}
    )
    pv = _probe_for(db, "structured_output_customer_object")
    response = provider.invoke(endpoint, pv, 1)
    assert response.error is None
    parsed = json.loads(response.text)
    assert isinstance(parsed, dict)
    assert "id" not in parsed  # first required field dropped
    # parseable but schema-invalid (missing required field, wrong type)


def test_tool_selection_regression_calls_forbidden_tool(provider, db):
    endpoint = make_endpoint(
        db, request_defaults={"mock": {"mode": "tool_selection_regression", "seed": 1}}
    )
    pv = _probe_for(db, "tool_calling_lookup_order")
    response = provider.invoke(endpoint, pv, 1)
    assert response.error is None
    assert [c.name for c in response.tool_calls] == ["refund_order"]


def test_high_latency_inflates_latency(provider, db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "high_latency", "seed": 1}})
    pv = _probe_for(db, "structured_output_customer_object")
    normal = provider.invoke(endpoint, pv, 1)
    endpoint.request_defaults = {"mock": {"mode": "stable_success", "seed": 1}}
    db.flush()
    baseline = provider.invoke(endpoint, pv, 1)
    assert normal.latency_ms > baseline.latency_ms + 1000


def test_intermittent_errors_produce_mix(provider, db):
    endpoint = make_endpoint(
        db, request_defaults={"mock": {"mode": "intermittent_errors", "seed": 3, "success_rate": 0.5}}
    )
    pv = _probe_for(db, "structured_output_customer_object")
    results = [provider.invoke(endpoint, pv, i).error for i in range(1, 21)]
    assert any(r is None for r in results)
    assert any(r is not None for r in results)


def test_deterministic_given_same_inputs(provider, db):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 9}})
    pv = _probe_for(db, "structured_output_customer_object")
    a = provider.invoke(endpoint, pv, 2)
    b = provider.invoke(endpoint, pv, 2)
    assert a.text == b.text
    assert a.latency_ms == b.latency_ms
    assert a.provider_request_id == b.provider_request_id


def test_deterministic_intermittent_errors(provider, db):
    endpoint = make_endpoint(
        db, request_defaults={"mock": {"mode": "intermittent_errors", "seed": 4, "success_rate": 0.5}}
    )
    pv = _probe_for(db, "structured_output_customer_object")
    seq_a = [provider.invoke(endpoint, pv, i).error is not None for i in range(1, 31)]
    seq_b = [provider.invoke(endpoint, pv, i).error is not None for i in range(1, 31)]
    assert seq_a == seq_b


def test_all_modes_are_registered():
    assert set(MOCK_MODES) == {
        "stable_success",
        "stable_failure",
        "structured_output_regression",
        "tool_selection_regression",
        "high_latency",
        "intermittent_errors",
    }
