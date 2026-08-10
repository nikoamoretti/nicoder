"""Helpers to build synthetic CanonicalProviderResponse objects for scorer tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from endpoint_ledger.runner.canonical_response import (
    CanonicalProviderResponse,
    ProviderError,
    ToolCall,
    Usage,
)


def resp(
    *,
    text: str | None = None,
    tool_calls: list[ToolCall] | None = None,
    latency_ms: int | None = 42,
    http_status: int | None = 200,
    error: ProviderError | None = None,
    input_tokens: int | None = 10,
    output_tokens: int | None = 10,
) -> CanonicalProviderResponse:
    now = datetime.now(UTC)
    return CanonicalProviderResponse(
        provider="test",
        model_requested="test-model",
        model_reported="test-model",
        request_started_at=now,
        request_completed_at=now,
        latency_ms=latency_ms,
        http_status=http_status,
        finish_reason="stop",
        text=text,
        tool_calls=tool_calls or [],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        error=error,
    )


def tool_call(name: str, arguments: Any, tool_id: str | None = "call_1") -> ToolCall:
    return ToolCall(id=tool_id, name=name, arguments=arguments)


CUSTOMER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "name"],
    "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
}
