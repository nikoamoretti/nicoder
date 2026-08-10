"""Unit tests for structured-output scorers."""

from __future__ import annotations

import pytest

from endpoint_ledger.scoring.structured_output import (
    JsonParseSuccess,
    JsonSchemaValidity,
    RequiredFieldPresence,
    UnexpectedFieldDetection,
)
from tests.scorer_utils import CUSTOMER_SCHEMA, resp


def _eb(**overrides):
    behavior = {
        "json_schema": CUSTOMER_SCHEMA,
        "required_fields": ["id", "name"],
        "example": {"id": "1", "name": "Taylor"},
    }
    behavior.update(overrides)
    return behavior


def test_json_parse_success_passes_on_valid_json():
    result = JsonParseSuccess().score(resp(text='{"a": 1}'), _eb())
    assert result.passed is True
    assert result.metric_name == "json_parse_success"
    assert result.numeric_value == 1.0


def test_json_parse_success_fails_on_prose():
    result = JsonParseSuccess().score(resp(text='here is {"a": 1}'), _eb())
    assert result.passed is False
    assert result.details["parse_error"]


def test_json_parse_success_fails_on_empty():
    result = JsonParseSuccess().score(resp(text=None), _eb())
    assert result.passed is False


def test_json_schema_validity_passes_conforming_instance():
    result = JsonSchemaValidity().score(resp(text='{"id": "1", "name": "Taylor"}'), _eb())
    assert result.passed is True


def test_json_schema_validity_fails_violating_instance():
    result = JsonSchemaValidity().score(resp(text='{"id": 123, "name": "Taylor"}'), _eb())
    assert result.passed is False


def test_json_schema_validity_skips_when_no_schema():
    result = JsonSchemaValidity().score(resp(text="{}"), {"no_schema": True})
    assert result.passed is None
    assert result.details.get("skipped") is True


def test_required_field_presence_missing_field():
    result = RequiredFieldPresence().score(resp(text='{"id": "1"}'), _eb())
    assert result.passed is False
    assert result.details["missing"] == ["name"]


def test_required_field_presence_all_present():
    result = RequiredFieldPresence().score(resp(text='{"id": "1", "name": "Taylor"}'), _eb())
    assert result.passed is True


def test_required_field_presence_rejects_non_object():
    result = RequiredFieldPresence().score(resp(text='["a"]'), _eb())
    assert result.passed is False


def test_unexpected_field_detection_flags_extra_field():
    result = UnexpectedFieldDetection().score(
        resp(text='{"id": "1", "name": "Taylor", "admin": true}'), _eb()
    )
    assert result.passed is False
    assert result.details["unexpected"] == ["admin"]


def test_unexpected_field_detection_passes_with_no_extras():
    result = UnexpectedFieldDetection().score(resp(text='{"id": "1", "name": "Taylor"}'), _eb())
    assert result.passed is True


@pytest.mark.parametrize(
    "scorer",
    [JsonParseSuccess(), JsonSchemaValidity(), RequiredFieldPresence(), UnexpectedFieldDetection()],
)
def test_scorers_have_stable_names_and_versions(scorer):
    assert isinstance(scorer.name, str) and scorer.name
    assert isinstance(scorer.version, str) and scorer.version
