"""Composite primary-outcome scorers.

Each baseline-eligible probe version defines exactly one primary metric: a
deterministic combination of its diagnostic checks. The composite is a pure
function of the same (response, expected_behavior) pair as any other scorer;
it never calls a model and never introduces hidden heuristics.
"""

from __future__ import annotations

from typing import Any

from endpoint_ledger.runner.canonical_response import CanonicalProviderResponse
from endpoint_ledger.scoring.base import Scorer, ScoreResult
from endpoint_ledger.scoring.instruction_adherence import (
    ConfirmationRequestDetection,
    ExactItemCount,
    ProhibitedExplanatoryProse,
)
from endpoint_ledger.scoring.structured_output import (
    JsonParseSuccess,
    JsonSchemaValidity,
    RequiredFieldPresence,
    UnexpectedFieldDetection,
)
from endpoint_ledger.scoring.tool_calling import (
    CorrectToolSelection,
    ForbiddenToolAvoidance,
    RequiredToolSequence,
    ToolArgumentEqualityOrSchema,
    ToolArgumentJsonValidity,
)


class CompositePrimaryScorer(Scorer):
    """Base class: passes all sub-checks through and requires every one."""

    sub_scorers: tuple[Scorer, ...] = ()

    def score(self, response: CanonicalProviderResponse, expected_behavior: dict[str, Any]) -> ScoreResult:
        sub_results = [scorer.score(response, expected_behavior) for scorer in self.sub_scorers]
        sub_summary = [
            {"scorer_name": r.scorer_name, "passed": r.passed, "explanation": r.explanation}
            for r in sub_results
        ]
        if any(r.passed is None for r in sub_results):
            passed: bool | None = None
        else:
            passed = all(bool(r.passed) for r in sub_results)
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            numeric_value=(1.0 if passed is True else 0.0 if passed is False else None),
            explanation=(
                "all component checks passed"
                if passed is True
                else f"{sum(1 for r in sub_results if r.passed is False)} of "
                f"{len(sub_results)} component checks failed"
                if passed is False
                else "component checks could not be fully evaluated"
            ),
            details={"components": sub_summary},
        )


class StructuredOutputTaskSuccess(CompositePrimaryScorer):
    name = "structured_output_task_success"
    metric_name = "structured_output_task_success"
    sub_scorers = (
        JsonParseSuccess(),
        JsonSchemaValidity(),
        RequiredFieldPresence(),
        UnexpectedFieldDetection(),
    )


class ToolTaskSuccess(CompositePrimaryScorer):
    name = "tool_task_success"
    metric_name = "tool_task_success"
    sub_scorers = (
        CorrectToolSelection(),
        ForbiddenToolAvoidance(),
        ToolArgumentJsonValidity(),
        ToolArgumentEqualityOrSchema(),
        RequiredToolSequence(),
    )


class InstructionItemCountSuccess(CompositePrimaryScorer):
    name = "instruction_item_count_success"
    metric_name = "instruction_item_count_success"
    sub_scorers = (
        JsonParseSuccess(),
        ExactItemCount(),
        ProhibitedExplanatoryProse(),
    )


class ConfirmationBehaviorSuccess(CompositePrimaryScorer):
    name = "confirmation_behavior_success"
    metric_name = "confirmation_behavior_success"
    sub_scorers = (
        JsonParseSuccess(),
        ConfirmationRequestDetection(),
        ProhibitedExplanatoryProse(),
    )
