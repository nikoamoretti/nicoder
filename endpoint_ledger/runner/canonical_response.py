"""Provider-neutral internal response object.

``CanonicalProviderResponse`` is the single in-memory representation every
provider adapter produces. The provider's raw response is preserved separately
from the normalized fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None


@dataclass
class ToolCall:
    id: str | None
    name: str
    arguments: Any

    @property
    def arguments_dict(self) -> dict[str, Any]:
        if isinstance(self.arguments, dict):
            return self.arguments
        if isinstance(self.arguments, str):
            try:
                parsed = json.loads(self.arguments)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class ProviderError:
    """Sanitized error information. Never contains secrets."""

    error_type: str
    message_sanitized: str
    http_status: int | None = None
    error_category: str | None = None
    error_code: str | None = None


@dataclass
class CanonicalProviderResponse:
    provider: str
    model_requested: str
    model_reported: str | None = None
    provider_request_id: str | None = None
    request_started_at: datetime | None = None
    request_completed_at: datetime | None = None
    latency_ms: int | None = None
    http_status: int | None = None
    finish_reason: str | None = None
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw_response: dict[str, Any] | None = None
    error: ProviderError | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    @property
    def tool_calls_dict(self) -> list[dict[str, Any]]:
        return [call.to_dict() for call in self.tool_calls]
