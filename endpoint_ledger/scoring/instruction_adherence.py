"""Deterministic scorers for instruction-adherence probes."""

from __future__ import annotations

import jsonschema

from endpoint_ledger.scoring._helpers import parse_json_text
from endpoint_ledger.scoring.base import Scorer, ScoreResult


class ExactItemCount(Scorer):
    name = "exact_item_count"
    metric_name = "exact_item_count"

    def score(self, response, expected_behavior) -> ScoreResult:
        expected_count = expected_behavior.get("item_count")
        if expected_count is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="probe does not declare item_count; skipped",
                details={"skipped": True},
            )
        parsed, error = parse_json_text(response.text)
        if parsed is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"cannot count items because response is not JSON: {error}",
                details={"skipped_unparseable": True, "item_count": expected_count},
            )
        if not isinstance(parsed, list):
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"expected a JSON array but got {type(parsed).__name__}",
                details={"item_count": expected_count},
            )
        passed = len(parsed) == expected_count
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                f"returned exactly {expected_count} items"
                if passed
                else f"expected {expected_count} items, got {len(parsed)}"
            ),
            details={"expected": expected_count, "actual": len(parsed)},
        )


class ProhibitedExplanatoryProse(Scorer):
    name = "prohibited_explanatory_prose"
    metric_name = "prohibited_explanatory_prose"

    def score(self, response, expected_behavior) -> ScoreResult:
        text = response.text
        parsed, error = parse_json_text(text)
        passed = parsed is not None
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "response is pure JSON with no explanatory prose"
                if passed
                else f"response contains explanatory prose and/or non-JSON content: {error}"
            ),
            details={"parse_error": error if not passed else None},
        )


class ConfirmationRequestDetection(Scorer):
    name = "confirmation_request_detection"
    metric_name = "confirmation_request_detection"

    def score(self, response, expected_behavior) -> ScoreResult:
        contract = expected_behavior.get("response_contract")
        if contract is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="probe does not declare response_contract; skipped",
                details={"skipped": True},
            )
        parsed, error = parse_json_text(response.text)
        if parsed is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"cannot evaluate contract because response is not JSON: {error}",
                details={"skipped_unparseable": True},
            )
        if not isinstance(parsed, dict):
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"expected a JSON object but got {type(parsed).__name__}",
                details={},
            )
        try:
            jsonschema.validate(instance=parsed, schema=contract)
        except jsonschema.ValidationError as exc:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"response violates the confirmation contract at {exc.json_path}: {exc.message}",
                details={"validation_error": exc.message, "path": exc.json_path},
            )
        checks: list[tuple[str, str]] = [
            ("expected_action", "action"),
            ("expected_action_status", "action_status"),
            ("expected_confirmation_required", "confirmation_required"),
        ]
        failures: list[dict] = []
        for expected_key, field in checks:
            expected_value = expected_behavior.get(expected_key)
            if expected_value is None:
                continue
            if parsed.get(field) != expected_value:
                failures.append(
                    {
                        "field": field,
                        "expected": expected_value,
                        "got": parsed.get(field),
                    }
                )
        passed = not failures
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "response matches the explicit confirmation contract"
                if passed
                else f"confirmation contract mismatches: {failures}"
            ),
            details={"failures": failures, "response": parsed},
        )
