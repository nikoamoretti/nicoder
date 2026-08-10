"""Scorer registry and scoring pipeline.

Importing this package registers every deterministic scorer. ``score_response``
runs exactly the scorers declared by a probe version, in the order declared,
and assigns each result a measurement role (primary / diagnostic /
informational).
"""

from __future__ import annotations

from typing import Any

from endpoint_ledger.measurement.roles import role_for_metric
from endpoint_ledger.runner.canonical_response import CanonicalProviderResponse
from endpoint_ledger.scoring.base import Scorer, ScoreRegistry, ScoreResult
from endpoint_ledger.scoring.composite import (
    ConfirmationBehaviorSuccess,
    InstructionItemCountSuccess,
    StructuredOutputTaskSuccess,
    ToolTaskSuccess,
)
from endpoint_ledger.scoring.instruction_adherence import (
    ConfirmationRequestDetection,
    ExactItemCount,
    ProhibitedExplanatoryProse,
)
from endpoint_ledger.scoring.operational import (
    ErrorOccurrence,
    HttpSuccess,
    LatencyMeasurement,
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

registry = ScoreRegistry()

_ALL: list[Scorer] = [
    # Composite primary outcomes.
    StructuredOutputTaskSuccess(),
    ToolTaskSuccess(),
    InstructionItemCountSuccess(),
    ConfirmationBehaviorSuccess(),
    # Diagnostics.
    JsonParseSuccess(),
    JsonSchemaValidity(),
    RequiredFieldPresence(),
    UnexpectedFieldDetection(),
    CorrectToolSelection(),
    ForbiddenToolAvoidance(),
    ToolArgumentJsonValidity(),
    ToolArgumentEqualityOrSchema(),
    RequiredToolSequence(),
    ExactItemCount(),
    ProhibitedExplanatoryProse(),
    ConfirmationRequestDetection(),
    # Operational.
    HttpSuccess(),
    ErrorOccurrence(),
    LatencyMeasurement(),
]

for _scorer in _ALL:
    registry.register(_scorer)


def score_response(
    response: CanonicalProviderResponse,
    scorer_names: list[str],
    expected_behavior: dict[str, Any],
    primary_metric: str | None = None,
) -> list[ScoreResult]:
    """Score one response against the probe's declared scorers.

    Each result carries its measurement ``role`` derived from the probe
    version's primary metric.
    """
    results: list[ScoreResult] = []
    for name in scorer_names:
        scorer = registry.get(name)
        if scorer is None:
            results.append(
                ScoreResult(
                    scorer_name=name,
                    scorer_version="unknown",
                    metric_name=name,
                    passed=None,
                    explanation=f"scorer {name!r} is not registered; skipped",
                    details={"missing": True},
                )
            )
            continue
        result = scorer.score(response, expected_behavior)
        result.role = role_for_metric(result.metric_name, primary_metric)
        results.append(result)
    return results


__all__ = [
    "ScoreRegistry",
    "ScoreResult",
    "Scorer",
    "registry",
    "score_response",
]
