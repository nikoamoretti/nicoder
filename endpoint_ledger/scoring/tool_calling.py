"""Deterministic scorers for tool-calling probes."""

from __future__ import annotations

from typing import Any

import jsonschema

from endpoint_ledger.scoring.base import Scorer, ScoreResult
from endpoint_ledger.scoring.structured_output import _normalize_arguments


class CorrectToolSelection(Scorer):
    name = "correct_tool_selection"
    metric_name = "correct_tool_selection"

    def score(self, response, expected_behavior) -> ScoreResult:
        expected = [c.get("name") for c in expected_behavior.get("expected_tool_calls") or []]
        found = [c.name for c in response.tool_calls]
        if not expected:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="probe does not declare expected_tool_calls; skipped",
                details={"skipped": True},
            )
        missing = [name for name in expected if name not in found]
        passed = not missing
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                f"all expected tools selected: {', '.join(expected)}"
                if passed
                else f"expected tools missing from response: {', '.join(missing)}"
            ),
            details={"expected_tools": expected, "found_tools": found, "missing": missing},
        )


class ForbiddenToolAvoidance(Scorer):
    name = "forbidden_tool_avoidance"
    metric_name = "forbidden_tool_avoidance"

    def score(self, response, expected_behavior) -> ScoreResult:
        forbidden = expected_behavior.get("forbidden_tools") or []
        found = [c.name for c in response.tool_calls]
        called = [name for name in forbidden if name in found]
        passed = not called
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "no forbidden tool called"
                if passed
                else f"forbidden tool(s) called: {', '.join(called)}"
            ),
            details={"forbidden_tools": forbidden, "called": called},
        )


class ToolArgumentJsonValidity(Scorer):
    name = "tool_argument_json_validity"
    metric_name = "tool_argument_json_validity"

    def score(self, response, expected_behavior) -> ScoreResult:
        if not response.tool_calls:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="no tool calls present to check; skipped",
                details={"skipped": True},
            )
        invalid: list[dict] = []
        for call in response.tool_calls:
            parsed = _normalize_arguments(call.arguments)
            if parsed is None:
                invalid.append({"name": call.name, "arguments": call.arguments})
        passed = not invalid
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "all tool arguments are valid JSON objects"
                if passed
                else f"{len(invalid)} tool call(s) have invalid arguments"
            ),
            details={"invalid_calls": invalid},
        )


class ToolArgumentEqualityOrSchema(Scorer):
    name = "tool_argument_equality_or_schema"
    metric_name = "tool_argument_equality_or_schema"

    def score(self, response, expected_behavior) -> ScoreResult:
        expected_calls = expected_behavior.get("expected_tool_calls") or []
        args_schema = expected_behavior.get("arguments_json_schema")
        exact = expected_behavior.get("exact_arguments", True)
        by_name: dict[str, Any] = {c.name: c for c in response.tool_calls}

        mismatches: list[dict] = []
        for expected in expected_calls:
            name = expected.get("name")
            found = by_name.get(name)
            if found is None:
                mismatches.append({"name": name, "issue": "tool not called"})
                continue
            got = _normalize_arguments(found.arguments)
            if args_schema is not None:
                try:
                    jsonschema.validate(instance=got, schema=args_schema)
                except jsonschema.ValidationError as exc:
                    mismatches.append(
                        {"name": name, "issue": "arguments violate schema", "error": exc.message}
                    )
            elif exact:
                expected_args = expected.get("arguments")
                if got != expected_args:
                    mismatches.append(
                        {
                            "name": name,
                            "issue": "arguments do not match exactly",
                            "expected": expected_args,
                            "got": got,
                        }
                    )
        passed = not mismatches
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation="all tool arguments match the expected contract" if passed
            else f"{len(mismatches)} tool argument mismatch(es)",
            details={"mismatches": mismatches},
        )


class RequiredToolSequence(Scorer):
    name = "required_tool_sequence"
    metric_name = "required_tool_sequence"

    def score(self, response, expected_behavior) -> ScoreResult:
        raw_sequence = expected_behavior.get("tool_call_sequence") or []
        sequence = [
            (c.get("name") if isinstance(c, dict) else c) for c in raw_sequence
        ]
        if not sequence:
            return ScoreResult(
                scorer_name=self.name,
                scorer_version=self.version,
                metric_name=self.metric_name,
                passed=None,
                explanation="probe does not declare tool_call_sequence; skipped",
                details={"skipped": True},
            )
        found = [c.name for c in response.tool_calls]
        passed = found == sequence
        return ScoreResult(
            scorer_name=self.name,
            scorer_version=self.version,
            metric_name=self.metric_name,
            passed=passed,
            explanation=(
                "tool calls match the required sequence in order"
                if passed
                else f"required sequence {sequence}, got {found}"
            ),
            details={"required": sequence, "found": found},
        )
