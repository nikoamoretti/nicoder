"""Score adjudication: failure classification for run audits.

An adjudication is a lightweight, separate record attached to a Score — the
original Score row is never overwritten. Classifications distinguish candidate
model behavior from measurement defects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from endpoint_ledger.db.models import Observation, ProbeVersion, Score

FailureClassification = str

CANDIDATE_MODEL_BEHAVIOR = "candidate_model_behavior"
PROBE_AMBIGUITY = "probe_ambiguity"
PROBE_INVALID_FOR_HARNESS = "probe_invalid_for_harness"
SCORER_BUG = "scorer_bug"
SCORER_TOO_BRITTLE = "scorer_too_brittle"
PROVIDER_NORMALIZATION_BUG = "provider_normalization_bug"
PROVIDER_ADAPTER_BUG = "provider_adapter_bug"
HARNESS_LIMITATION = "harness_limitation"
EXPECTED_NONDETERMINISM = "expected_nondeterminism"
ACCOUNT_CONFIGURATION = "account_configuration"
PROVIDER_TRANSPORT = "provider_transport"
UNRESOLVED = "unresolved"

FAILURE_CLASSIFICATIONS: tuple[str, ...] = (
    CANDIDATE_MODEL_BEHAVIOR,
    PROBE_AMBIGUITY,
    PROBE_INVALID_FOR_HARNESS,
    SCORER_BUG,
    SCORER_TOO_BRITTLE,
    PROVIDER_NORMALIZATION_BUG,
    PROVIDER_ADAPTER_BUG,
    HARNESS_LIMITATION,
    EXPECTED_NONDETERMINISM,
    ACCOUNT_CONFIGURATION,
    PROVIDER_TRANSPORT,
    UNRESOLVED,
)

# Classes that indicate a measurement defect rather than model behavior.
MEASUREMENT_DEFECT_CLASSES: frozenset[str] = frozenset(
    {
        PROBE_AMBIGUITY,
        PROBE_INVALID_FOR_HARNESS,
        SCORER_BUG,
        SCORER_TOO_BRITTLE,
        PROVIDER_NORMALIZATION_BUG,
        PROVIDER_ADAPTER_BUG,
        HARNESS_LIMITATION,
        UNRESOLVED,
    }
)


def default_failure_classification(
    score: Score,
    observation: Observation,
    probe_version: ProbeVersion,
) -> tuple[str, str]:
    """Deterministic preliminary classification for a failed scored check.

    Rule-based, not a judgement call by a model: it inspects the probe version's
    declared ineligibility and the scorer's structured details.
    """
    reason = probe_version.ineligibility_reason or ""

    if not probe_version.baseline_eligible and "harness" in reason:
        return PROBE_INVALID_FOR_HARNESS, (
            f"probe version v{probe_version.version_number} is marked invalid for the "
            "harness: the recorded harness performs a single provider invocation per "
            "observation and cannot execute the behavior this probe requires"
        )

    if score.metric_name in {
        "required_tool_sequence",
        "tool_argument_equality_or_schema",
        "correct_tool_selection",
    } and not probe_version.baseline_eligible:
        return HARNESS_LIMITATION, (
            f"the {score.metric_name} check requires tool-call evidence the single-"
            "invocation harness never produces (no tool-result turn is sent back to the model)"
        )

    details: dict[str, Any] = score.details or {}
    if score.metric_name == "confirmation_request_detection":
        failures = details.get("failures") or []
        fields = {f.get("field") for f in failures}
        if "action" in fields:
            return SCORER_TOO_BRITTLE, (
                "the check requires an exact 'action' identifier value that the probe "
                "prompt never specifies; the model correctly blocked the action and "
                "requested confirmation, so the mismatch measures prompt completeness, "
                "not model behavior"
            )

    if score.metric_name in {"json_schema_validity", "required_field_presence"}:
        return CANDIDATE_MODEL_BEHAVIOR, (
            "the response violates the declared JSON Schema / required fields; "
            "consistent with genuine model behavior failure unless a defect is found"
        )

    return UNRESOLVED, "no rule matched; requires human review"


def persist_adjudications(
    session: Session,
    adjudications: list[tuple[Score, str, str]],
    reviewer_label: str,
) -> None:
    """Persist adjudications without touching the original Score rows."""
    from endpoint_ledger.db.models import ScoreAdjudication

    now = datetime.now(UTC)
    for score, classification, notes in adjudications:
        session.add(
            ScoreAdjudication(
                score_id=score.id,
                classification=classification,
                notes=notes,
                reviewed_at=now,
                reviewer_label=reviewer_label,
            )
        )
    session.commit()
