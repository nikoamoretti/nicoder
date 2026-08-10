"""Unit tests for tool-calling scorers."""

from __future__ import annotations

from endpoint_ledger.scoring.tool_calling import (
    CorrectToolSelection,
    ForbiddenToolAvoidance,
    RequiredToolSequence,
    ToolArgumentEqualityOrSchema,
    ToolArgumentJsonValidity,
)
from tests.scorer_utils import resp, tool_call

EB = {
    "expected_tool_calls": [{"name": "lookup_order", "arguments": {"order_id": "ORD-1"}}],
    "forbidden_tools": ["refund_order"],
    "tool_call_sequence": ["lookup_order"],
    "exact_arguments": True,
}


def test_correct_tool_selection_passes():
    r = resp(tool_calls=[tool_call("lookup_order", {"order_id": "ORD-1"})])
    assert CorrectToolSelection().score(r, EB).passed is True


def test_correct_tool_selection_fails_on_wrong_tool():
    r = resp(tool_calls=[tool_call("delete_order", {"order_id": "ORD-1"})])
    result = CorrectToolSelection().score(r, EB)
    assert result.passed is False
    assert result.details["missing"] == ["lookup_order"]


def test_forbidden_tool_avoidance_fails_when_forbidden_called():
    r = resp(tool_calls=[tool_call("refund_order", {"order_id": "ORD-1"})])
    result = ForbiddenToolAvoidance().score(r, EB)
    assert result.passed is False
    assert result.details["called"] == ["refund_order"]


def test_forbidden_tool_avoidance_passes():
    r = resp(tool_calls=[tool_call("lookup_order", {"order_id": "ORD-1"})])
    assert ForbiddenToolAvoidance().score(r, EB).passed is True


def test_tool_argument_json_validity_dict_ok():
    r = resp(tool_calls=[tool_call("lookup_order", {"order_id": "ORD-1"})])
    assert ToolArgumentJsonValidity().score(r, EB).passed is True


def test_tool_argument_json_validity_string_ok():
    r = resp(tool_calls=[tool_call("lookup_order", '{"order_id": "ORD-1"}')])
    assert ToolArgumentJsonValidity().score(r, EB).passed is True


def test_tool_argument_json_validity_bad_arguments():
    r = resp(tool_calls=[tool_call("lookup_order", "not json {")])
    result = ToolArgumentJsonValidity().score(r, EB)
    assert result.passed is False
    assert len(result.details["invalid_calls"]) == 1


def test_tool_argument_equality_exact_match():
    r = resp(tool_calls=[tool_call("lookup_order", {"order_id": "ORD-1"})])
    assert ToolArgumentEqualityOrSchema().score(r, EB).passed is True


def test_tool_argument_equality_mismatch():
    r = resp(tool_calls=[tool_call("lookup_order", {"order_id": "ORD-999"})])
    result = ToolArgumentEqualityOrSchema().score(r, EB)
    assert result.passed is False
    assert result.details["mismatches"][0]["issue"] == "arguments do not match exactly"


def test_tool_argument_equality_schema_validation():
    schema_eb = {
        **EB,
        "arguments_json_schema": {
            "type": "object",
            "required": ["order_id"],
            "properties": {"order_id": {"type": "string"}},
        },
    }
    r = resp(tool_calls=[tool_call("lookup_order", {"order_id": "ORD-1"})])
    assert ToolArgumentEqualityOrSchema().score(r, schema_eb).passed is True
    bad = resp(tool_calls=[tool_call("lookup_order", {"order_id": 42})])
    assert ToolArgumentEqualityOrSchema().score(bad, schema_eb).passed is False


def test_required_tool_sequence_passes_in_order():
    seq_eb = {**EB, "tool_call_sequence": ["lookup_subscription", "cancel_subscription"]}
    r = resp(
        tool_calls=[
            tool_call("lookup_subscription", {"subscription_id": "S-1"}),
            tool_call("cancel_subscription", {"subscription_id": "S-1"}),
        ]
    )
    assert RequiredToolSequence().score(r, seq_eb).passed is True


def test_required_tool_sequence_fails_on_wrong_order():
    seq_eb = {**EB, "tool_call_sequence": ["lookup_subscription", "cancel_subscription"]}
    r = resp(
        tool_calls=[
            tool_call("cancel_subscription", {"subscription_id": "S-1"}),
            tool_call("lookup_subscription", {"subscription_id": "S-1"}),
        ]
    )
    result = RequiredToolSequence().score(r, seq_eb)
    assert result.passed is False
    assert result.details["required"] == ["lookup_subscription", "cancel_subscription"]


def test_required_tool_sequence_accepts_string_sequence():
    str_eb = {**EB, "tool_call_sequence": ["lookup_subscription", "cancel_subscription"]}
    r = resp(tool_calls=[tool_call("lookup_subscription", {}), tool_call("cancel_subscription", {})])
    assert RequiredToolSequence().score(r, str_eb).passed is True
