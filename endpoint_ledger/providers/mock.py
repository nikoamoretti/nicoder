"""Deterministic mock provider.

The mock simulates a real endpoint with switchable, reproducible scenario
modes. Behavior is a pure function of
(seed, endpoint id, probe version id, repeat index) so the same inputs always
produce the same outputs.

Scenario modes (configured under ``request_defaults["mock"]``):

- ``stable_success``            — every probe returns its expected behavior.
- ``stable_failure``            — every request fails with HTTP 500.
- ``structured_output_regression`` — structured-output probes return JSON that
  violates their schema/required fields.
- ``tool_selection_regression`` — tool-calling probes call a forbidden tool.
- ``high_latency``              — success, but latency is inflated.
- ``intermittent_errors``       — seeded random success/failure at
  ``success_rate``.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Any

from endpoint_ledger.db.models import ProbeVersion, ProviderEndpoint
from endpoint_ledger.providers.base import ProviderAdapter
from endpoint_ledger.runner.canonical_response import (
    CanonicalProviderResponse,
    ProviderError,
    ToolCall,
    Usage,
)

STABLE_SUCCESS = "stable_success"
STABLE_FAILURE = "stable_failure"
STRUCTURED_OUTPUT_REGRESSION = "structured_output_regression"
TOOL_SELECTION_REGRESSION = "tool_selection_regression"
HIGH_LATENCY = "high_latency"
INTERMITTENT_ERRORS = "intermittent_errors"

MOCK_MODES = (
    STABLE_SUCCESS,
    STABLE_FAILURE,
    STRUCTURED_OUTPUT_REGRESSION,
    TOOL_SELECTION_REGRESSION,
    HIGH_LATENCY,
    INTERMITTENT_ERRORS,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MockProvider(ProviderAdapter):
    provider_name = "mock"

    def __init__(
        self,
        latency_base_ms: int | None = None,
        latency_jitter_ms: int | None = None,
    ) -> None:
        self._latency_base_ms = latency_base_ms
        self._latency_jitter_ms = latency_jitter_ms

    def is_available(self) -> bool:
        return True

    def invoke(
        self,
        endpoint_config: ProviderEndpoint,
        probe_version: ProbeVersion,
        repeat_index: int,
    ) -> CanonicalProviderResponse:
        cfg: dict[str, Any] = endpoint_config.request_defaults.get("mock") or {}
        mode = cfg.get("mode", STABLE_SUCCESS)
        if mode not in MOCK_MODES:
            mode = STABLE_SUCCESS
        seed = int(cfg.get("seed", 1))
        success_rate = float(cfg.get("success_rate", 0.95))
        extra_latency_ms = int(cfg.get("regression_latency_ms", 2000))
        rng = random.Random(f"{seed}:{endpoint_config.id}:{probe_version.id}:{repeat_index}")

        latency_base = self._latency_base_ms or int(cfg.get("latency_ms_base", 0)) or 120
        latency_jitter = self._latency_jitter_ms or int(cfg.get("latency_ms_jitter", 0)) or 15

        started = _utcnow()
        latency = latency_base + rng.randint(0, latency_jitter)

        if mode == STABLE_FAILURE:
            completed = _utcnow()
            return self._error(
                provider="mock",
                model_requested=endpoint_config.model_requested,
                error_type="server_error",
                message="simulated stable provider failure",
                http_status=500,
                started=started,
                completed=completed,
            )

        if mode == INTERMITTENT_ERRORS and rng.random() > success_rate:
            completed = _utcnow()
            return self._error(
                provider="mock",
                model_requested=endpoint_config.model_requested,
                error_type="server_error",
                message="simulated intermittent provider failure",
                http_status=503,
                started=started,
                completed=completed,
            )

        content = self._build_success_content(probe_version, rng)
        if mode == STRUCTURED_OUTPUT_REGRESSION:
            content = self._apply_structured_regression(probe_version, content, rng)
        elif mode == TOOL_SELECTION_REGRESSION:
            content = self._apply_tool_regression(probe_version, content, rng)
        elif mode == HIGH_LATENCY:
            latency += extra_latency_ms

        completed = _utcnow()
        response = CanonicalProviderResponse(
            provider="mock",
            model_requested=endpoint_config.model_requested,
            model_reported=content["model_reported"],
            provider_request_id=(
                f"mockreq-{seed}-{endpoint_config.id}-{probe_version.id}-{repeat_index}"
            ),
            request_started_at=started,
            request_completed_at=completed,
            latency_ms=latency,
            http_status=200,
            finish_reason="stop",
            text=content["text"],
            tool_calls=content["tool_calls"],
            usage=content["usage"],
            raw_response=content["raw_response"],
        )
        return response

    # ------------------------------------------------------------------ utils

    def _build_success_content(
        self, probe_version: ProbeVersion, rng: random.Random
    ) -> dict[str, Any]:
        eb = probe_version.expected_behavior
        category = probe_version.probe.category if probe_version.probe else ""

        text: str | None = None
        tool_calls: list[ToolCall] = []
        raw: dict[str, Any]

        if category == "tool_calling":
            calls = eb.get("example_tool_calls") or []
            for call in calls:
                tool_calls.append(
                    ToolCall(
                        id=call.get("id"),
                        name=call["name"],
                        arguments=call.get("arguments"),
                    )
                )
            raw = {
                "provider": "mock",
                "tool_calls": [c.to_dict() for c in tool_calls],
            }
        else:
            example = eb.get("example")
            text = json.dumps(example, sort_keys=True)
            raw = {"provider": "mock", "text": text}

        usage = Usage(
            input_tokens=20 + rng.randint(0, 40),
            output_tokens=30 + rng.randint(0, 60),
            total_tokens=50 + rng.randint(0, 100),
        )
        return {
            "text": text,
            "tool_calls": tool_calls,
            "usage": usage,
            "raw_response": raw,
            "model_reported": "mock:model-1",
        }

    def _apply_structured_regression(
        self,
        probe_version: ProbeVersion,
        content: dict[str, Any],
        rng: random.Random,
    ) -> dict[str, Any]:
        """Return JSON that still parses but violates the schema."""
        eb = probe_version.expected_behavior
        required = eb.get("required_fields") or []
        example = eb.get("example")
        if not isinstance(example, dict) or not required:
            return content
        mutated = dict(example)
        drop = required[0]
        mutated.pop(drop, None)
        if len(required) > 1:
            key = required[1]
            if key in mutated:
                current = mutated[key]
                if isinstance(current, bool):
                    mutated[key] = "not-a-boolean"
                elif isinstance(current, int):
                    mutated[key] = "not-an-integer"
                else:
                    mutated[key] = 12345
        text = json.dumps(mutated, sort_keys=True)
        content["text"] = text
        content["raw_response"] = {"provider": "mock", "text": text, "regression": "structured_output"}
        return content

    def _apply_tool_regression(
        self,
        probe_version: ProbeVersion,
        content: dict[str, Any],
        rng: random.Random,
    ) -> dict[str, Any]:
        """Call the first forbidden tool with the expected arguments."""
        eb = probe_version.expected_behavior
        forbidden = eb.get("forbidden_tools") or []
        expected_calls = eb.get("expected_tool_calls") or []
        if not forbidden or not expected_calls:
            return content
        args = expected_calls[0].get("arguments") or {}
        calls = [
            ToolCall(
                id=f"call_mock_reg_{i}",
                name=forbidden[0],
                arguments=args,
            )
            for i in range(1)
        ]
        content["tool_calls"] = calls
        content["raw_response"] = {
            "provider": "mock",
            "tool_calls": [c.to_dict() for c in calls],
            "regression": "tool_selection",
        }
        return content

    def _error(
        self,
        provider: str,
        model_requested: str,
        error_type: str,
        message: str,
        http_status: int,
        started: datetime,
        completed: datetime,
    ) -> CanonicalProviderResponse:
        latency = int((completed - started).total_seconds() * 1000)
        return CanonicalProviderResponse(
            provider=provider,
            model_requested=model_requested,
            request_started_at=started,
            request_completed_at=completed,
            latency_ms=latency,
            http_status=http_status,
            error=ProviderError(
                error_type=error_type,
                message_sanitized=message,
                http_status=http_status,
            ),
        )
