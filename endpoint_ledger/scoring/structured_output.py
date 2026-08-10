"""Deterministic scorers for structured-output probes."""

from __future__ import annotations

from typing import Any

import jsonschema

from endpoint_ledger.scoring._helpers import parse_json_text
from endpoint_ledger.scoring.base import Scorer, ScoreResult


class JsonParseSuccess(Scorer):
    name = "json_parse_success"
    metric_name = "json_parse_success"

    def score(self, response, expected_behavior) -> ScoreResult:
        parsed, error = parse_json_text(response.text)
        passed = parsed is not None
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            numeric_value=1.0 if passed else 0.0,
            explanation="response text is valid JSON" if passed else f"response text is not valid JSON: {error}",
            details={"parse_error": error} if error else {"parsed_type": type(parsed).__name__},
        )


class JsonSchemaValidity(Scorer):
    name = "json_schema_validity"
    metric_name = "json_schema_validity"

    def score(self, response, expected_behavior) -> ScoreResult:
        schema = expected_behavior.get("json_schema")
        if schema is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="probe does not declare a json_schema; skipped",
                details={"skipped": True},
            )
        parsed, error = parse_json_text(response.text)
        if parsed is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"cannot validate schema because response is not JSON: {error}",
                details={"skipped_unparseable": True},
            )
        try:
            jsonschema.validate(instance=parsed, schema=schema)
        except jsonschema.ValidationError as exc:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"instance violates JSON Schema at {exc.json_path}: {exc.message}",
                details={"validation_error": str(exc.message), "path": exc.json_path},
            )
        except jsonschema.SchemaError as exc:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation=f"probe schema itself is invalid; scorer skipped: {exc.message}",
                details={"schema_error": str(exc.message)},
            )
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=True,
            explanation="instance satisfies the declared JSON Schema",
            details={"valid": True},
        )


class RequiredFieldPresence(Scorer):
    name = "required_field_presence"
    metric_name = "required_field_presence"

    def score(self, response, expected_behavior) -> ScoreResult:
        required = list(expected_behavior.get("required_fields") or [])
        parsed, error = parse_json_text(response.text)
        if parsed is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"cannot check fields because response is not JSON: {error}",
                details={"skipped_unparseable": True, "required_fields": required},
            )
        if not isinstance(parsed, dict):
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"expected a JSON object but got {type(parsed).__name__}",
                details={"required_fields": required},
            )
        missing = [field for field in required if field not in parsed]
        passed = not missing
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                f"all {len(required)} required fields present"
                if passed
                else f"missing required fields: {', '.join(missing)}"
            ),
            details={"missing": missing, "required_fields": required},
        )


class UnexpectedFieldDetection(Scorer):
    name = "unexpected_field_detection"
    metric_name = "unexpected_field_detection"

    def score(self, response, expected_behavior) -> ScoreResult:
        schema = expected_behavior.get("json_schema") or {}
        properties = schema.get("properties") or {}
        allowed = set(properties) | set(expected_behavior.get("allowed_fields") or [])
        parsed, error = parse_json_text(response.text)
        if parsed is None:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=False,
                explanation=f"cannot check unexpected fields because response is not JSON: {error}",
                details={"skipped_unparseable": True},
            )
        if not isinstance(parsed, dict):
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="response is not an object; unexpected-field check skipped",
                details={"skipped": True},
            )
        unexpected = sorted(set(parsed) - allowed)
        passed = not unexpected
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "no unexpected fields present"
                if passed
                else f"unexpected fields present: {', '.join(unexpected)}"
            ),
            details={"unexpected": unexpected, "allowed_count": len(allowed)},
        )


def _normalize_arguments(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        parsed, _ = parse_json_text(arguments)
        return parsed if isinstance(parsed, dict) else None
    return None
