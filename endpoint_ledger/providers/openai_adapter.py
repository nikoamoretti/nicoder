"""OpenAI provider adapter.

- Reads ``OPENAI_API_KEY`` from the environment. When absent the adapter
  reports itself unavailable and raises ``ProviderUnavailableError`` on invoke.
- Uses the official ``openai`` SDK. Retries only transient failures with
  bounded exponential backoff; validation-style failures are never retried.
- Preserves the provider's raw response alongside normalized fields.
- Never logs the API key and never stores it anywhere.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from endpoint_ledger.config import Settings
from endpoint_ledger.db.models import ProbeVersion, ProviderEndpoint
from endpoint_ledger.providers.base import ProviderAdapter, ProviderUnavailableError
from endpoint_ledger.runner.canonical_response import (
    CanonicalProviderResponse,
    ProviderError,
    ToolCall,
    Usage,
)
from endpoint_ledger.sanitize import sanitize_message

NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 409, 422}

SleepFn = Callable[[float], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def should_retry(exc: Exception) -> bool:
    """Whether a provider exception is safe to retry.

    Transient transport failures and 5xx/429 are retryable. Validation,
    authentication, permission and not-found failures are never retried.
    """
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, (RateLimitError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code not in NON_RETRYABLE_STATUS_CODES
    if isinstance(exc, OpenAIError):
        return True
    return False


def compute_backoff_seconds(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Bounded exponential backoff with jitter for ``attempt`` (1-based)."""
    exponential = min(cap, base * (2 ** (attempt - 1)))
    jitter = 0.5 + random.random() * 0.5
    return round(exponential * jitter, 3)


def normalize_openai_response(
    raw: dict[str, Any],
    model_requested: str,
    started: datetime,
    completed: datetime,
) -> CanonicalProviderResponse:
    """Normalize a raw OpenAI chat-completion dict into a canonical response."""
    latency_ms = int(max(0, (completed - started).total_seconds() * 1000))
    choices = raw.get("choices") or []
    choice: dict[str, Any] = choices[0] if choices else {}
    message: dict[str, Any] = choice.get("message") or {}
    usage_raw: dict[str, Any] = raw.get("usage") or {}

    tool_calls_raw = message.get("tool_calls") or []
    tool_calls: list[ToolCall] = []
    for tc in tool_calls_raw:
        function = tc.get("function") or {}
        tool_calls.append(
            ToolCall(
                id=tc.get("id"),
                name=function.get("name", ""),
                arguments=function.get("arguments"),
            )
        )

    return CanonicalProviderResponse(
        provider="openai",
        model_requested=model_requested,
        model_reported=raw.get("model"),
        provider_request_id=raw.get("id"),
        request_started_at=started,
        request_completed_at=completed,
        latency_ms=latency_ms,
        http_status=200,
        finish_reason=choice.get("finish_reason"),
        text=message.get("content"),
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage_raw.get("prompt_tokens"),
            output_tokens=usage_raw.get("completion_tokens"),
            total_tokens=usage_raw.get("total_tokens"),
        ),
        raw_response=raw,
    )


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"

    def __init__(
        self,
        settings: Settings | None = None,
        sleep_fn: SleepFn = time.sleep,
        client_factory: Callable[[str, float], OpenAI] | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self._sleep_fn = sleep_fn
        self._client_factory = client_factory

    def is_available(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    def invoke(
        self,
        endpoint_config: ProviderEndpoint,
        probe_version: ProbeVersion,
        repeat_index: int,
    ) -> CanonicalProviderResponse:
        if not self.is_available():
            raise ProviderUnavailableError(
                "OPENAI_API_KEY is not set; the openai endpoint is unavailable"
            )
        api_key = os.getenv("OPENAI_API_KEY") or ""
        if self._client_factory is not None:
            client = self._client_factory(api_key, self._settings.openai_timeout_seconds)
        else:
            client = OpenAI(api_key=api_key, timeout=self._settings.openai_timeout_seconds, max_retries=0)

        payload = self._build_payload(endpoint_config, probe_version)
        started = _utcnow()
        attempt = 0
        while True:
            try:
                completion = client.chat.completions.create(**payload)
                raw = completion.model_dump()
                completed = _utcnow()
                return normalize_openai_response(raw, endpoint_config.model_requested, started, completed)
            except OpenAIError as exc:
                completed = _utcnow()
                if should_retry(exc) and attempt < self._settings.openai_max_retries:
                    attempt += 1
                    self._sleep_fn(compute_backoff_seconds(attempt))
                    continue
                return self._error_response(exc, endpoint_config.model_requested, started, completed)

    @staticmethod
    def _build_payload(
        endpoint_config: ProviderEndpoint,
        probe_version: ProbeVersion,
    ) -> dict[str, Any]:
        messages = [
            {"role": m.get("role", "user"), "content": m.get("content")}
            for m in probe_version.prompt_messages
        ]
        params = dict(endpoint_config.request_defaults.get("request") or {})
        params.update(probe_version.request_parameters or {})
        model = params.pop("model", None) or endpoint_config.model_requested
        payload: dict[str, Any] = {"model": model, "messages": messages}
        payload.update(params)
        if probe_version.tools:
            payload["tools"] = probe_version.tools
            payload.setdefault("tool_choice", "auto")
        return payload

    @staticmethod
    def _error_response(
        exc: Exception,
        model_requested: str,
        started: datetime,
        completed: datetime,
    ) -> CanonicalProviderResponse:
        from endpoint_ledger.measurement.taxonomy import classify_provider_error

        latency_ms = int(max(0, (completed - started).total_seconds() * 1000))
        status = getattr(exc, "status_code", None)
        sanitized = sanitize_message(str(exc))
        category, code = classify_provider_error(type(exc).__name__, sanitized, status)
        return CanonicalProviderResponse(
            provider="openai",
            model_requested=model_requested,
            request_started_at=started,
            request_completed_at=completed,
            latency_ms=latency_ms,
            http_status=status,
            error=ProviderError(
                error_type=type(exc).__name__,
                message_sanitized=sanitized,
                http_status=status,
                error_category=category,
                error_code=code,
            ),
        )
