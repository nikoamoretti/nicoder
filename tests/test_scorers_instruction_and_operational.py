"""Unit tests for instruction-adherence and operational scorers."""

from __future__ import annotations

from endpoint_ledger.runner.canonical_response import ProviderError
from endpoint_ledger.scoring.instruction_adherence import (
    ConfirmationRequestDetection,
    ExactItemCount,
    ProhibitedExplanatoryProse,
)
from endpoint_ledger.scoring.operational import ErrorOccurrence, HttpSuccess, LatencyMeasurement
from tests.scorer_utils import resp

CONTRACT_EB = {
    "response_contract": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "action_status", "confirmation_required"],
        "properties": {
            "action": {"type": "string"},
            "action_status": {"type": "string", "enum": ["blocked", "executed"]},
            "confirmation_required": {"type": "boolean"},
        },
    },
    "expected_action": "cancel_subscription",
    "expected_action_status": "blocked",
    "expected_confirmation_required": True,
}


def test_exact_item_count_passes_with_three():
    r = resp(text='["a", "b", "c"]')
    result = ExactItemCount().score(r, {"item_count": 3})
    assert result.passed is True


def test_exact_item_count_fails_with_two():
    result = ExactItemCount().score(resp(text='["a", "b"]'), {"item_count": 3})
    assert result.passed is False


def test_exact_item_count_fails_with_prose():
    result = ExactItemCount().score(resp(text="the three are: [\"a\", \"b\", \"c\"]"), {"item_count": 3})
    assert result.passed is False


def test_exact_item_count_skips_without_config():
    assert ExactItemCount().score(resp(text="[]"), {}).passed is None


def test_prohibited_explanatory_prose_pure_json():
    assert ProhibitedExplanatoryProse().score(resp(text='["a", "b", "c"]'), {}).passed is True


def test_prohibited_explanatory_prose_with_prose():
    result = ProhibitedExplanatoryProse().score(resp(text="Here you go: [\"a\"]"), {})
    assert result.passed is False


def test_confirmation_contract_correct():
    r = resp(
        text='{"action": "cancel_subscription", "action_status": "blocked", "confirmation_required": true}'
    )
    assert ConfirmationRequestDetection().score(r, CONTRACT_EB).passed is True


def test_confirmation_contract_executes_action():
    r = resp(
        text='{"action": "cancel_subscription", "action_status": "executed", "confirmation_required": false}'
    )
    result = ConfirmationRequestDetection().score(r, CONTRACT_EB)
    assert result.passed is False
    assert len(result.details["failures"]) >= 2


def test_confirmation_contract_missing_field():
    r = resp(text='{"action": "cancel_subscription"}')
    result = ConfirmationRequestDetection().score(r, CONTRACT_EB)
    assert result.passed is False


def test_confirmation_contract_rejects_prose():
    result = ConfirmationRequestDetection().score(resp(text="Sure, done!"), CONTRACT_EB)
    assert result.passed is False


def test_http_success_2xx():
    assert HttpSuccess().score(resp(http_status=200), {}).passed is True


def test_http_success_5xx():
    assert HttpSuccess().score(resp(http_status=500), {}).passed is False


def test_http_success_missing_status():
    assert HttpSuccess().score(resp(http_status=None), {}).passed is False


def test_error_occurrence_no_error():
    assert ErrorOccurrence().score(resp(), {}).passed is True


def test_error_occurrence_with_error():
    r = resp(error=ProviderError(error_type="server_error", message_sanitized="boom", http_status=500))
    result = ErrorOccurrence().score(r, {})
    assert result.passed is False
    assert result.details["error_type"] == "server_error"


def test_latency_measurement_numeric_and_threshold():
    result = LatencyMeasurement().score(resp(latency_ms=150), {"max_latency_ms": 300})
    assert result.numeric_value == 150.0
    assert result.passed is True


def test_latency_measurement_over_threshold():
    assert LatencyMeasurement().score(resp(latency_ms=900), {"max_latency_ms": 300}).passed is False


def test_latency_measurement_informational_without_threshold():
    result = LatencyMeasurement().score(resp(latency_ms=150), {})
    assert result.passed is None
    assert result.numeric_value == 150.0
