"""Score roles and the primary-outcome contract.

Every baseline-eligible probe version defines exactly one *primary* outcome
metric. Diagnostic metrics explain failures but never receive independent
weight in an overall pass rate. Informational metrics (latency, tokens, cost)
never count as passing or failing behavior.
"""

from __future__ import annotations

from typing import Any, Literal

ScoreRole = Literal["primary", "diagnostic", "informational"]

PRIMARY_ROLE: ScoreRole = "primary"
DIAGNOSTIC_ROLE: ScoreRole = "diagnostic"
INFORMATIONAL_ROLE: ScoreRole = "informational"

# Metrics that are never behavioral pass/fail signals.
INFORMATIONAL_METRICS: frozenset[str] = frozenset({"latency_measurement"})

# Composite primary metric names -> the deterministic sub-metrics they combine.
# Used both by the composite scorers and to derive a primary outcome for
# legacy observations that predate explicit primary scores.
COMPOSITE_PRIMARY_DEFS: dict[str, list[str]] = {
    "structured_output_task_success": [
        "json_parse_success",
        "json_schema_validity",
        "required_field_presence",
        "unexpected_field_detection",
    ],
    "tool_task_success": [
        "correct_tool_selection",
        "forbidden_tool_avoidance",
        "tool_argument_json_validity",
        "tool_argument_equality_or_schema",
        "required_tool_sequence",
    ],
    "instruction_item_count_success": [
        "json_parse_success",
        "exact_item_count",
        "prohibited_explanatory_prose",
    ],
    "confirmation_behavior_success": [
        "json_parse_success",
        "confirmation_request_detection",
        "prohibited_explanatory_prose",
    ],
}


def primary_metric_for_definition(category: str, expected_behavior: dict[str, Any]) -> str:
    """The primary metric name for a probe definition (canonical mapping)."""
    if category == "structured_output":
        return "structured_output_task_success"
    if category == "tool_calling":
        return "tool_task_success"
    if expected_behavior.get("item_count") is not None:
        return "instruction_item_count_success"
    return "confirmation_behavior_success"


def role_for_metric(
    metric_name: str,
    primary_metric: str | None,
) -> ScoreRole:
    """Role for a stored score, derived from the probe version's primary metric."""
    if metric_name == primary_metric:
        return PRIMARY_ROLE
    if metric_name in INFORMATIONAL_METRICS:
        return INFORMATIONAL_ROLE
    return DIAGNOSTIC_ROLE
