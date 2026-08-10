"""Structured error taxonomy for provider failures.

Separates account/client configuration problems from provider transport issues,
completed-model responses with behavioral failures, and unknown conditions.
``insufficient_quota`` is an account configuration incident, never a generic
rate limit.
"""

from __future__ import annotations

from typing import Literal

ErrorCategory = Literal["account_configuration", "provider_transport", "model_behavior", "unknown"]

ACCOUNT_CONFIGURATION: ErrorCategory = "account_configuration"
PROVIDER_TRANSPORT: ErrorCategory = "provider_transport"
MODEL_BEHAVIOR: ErrorCategory = "model_behavior"
UNKNOWN: ErrorCategory = "unknown"

# Account / client configuration codes.
ACCOUNT_CODES = {
    "missing_api_key",
    "invalid_api_key",
    "insufficient_quota",
    "account_disabled",
    "invalid_account_configuration",
}

# Provider transport / service codes.
TRANSPORT_CODES = {
    "provider_rate_limit",
    "provider_timeout",
    "provider_5xx",
    "provider_connection_error",
    "malformed_provider_response",
}

# Completed model behavior codes.
MODEL_BEHAVIOR_CODES = {"valid_provider_response_with_behavioral_failure"}

UNCLASSIFIED_CODE = "unclassified_error"

ERROR_CODES: frozenset[str] = frozenset(ACCOUNT_CODES | TRANSPORT_CODES | MODEL_BEHAVIOR_CODES)


def classify_provider_error(
    error_type: str | None,
    message_sanitized: str | None,
    http_status: int | None,
) -> tuple[ErrorCategory, str]:
    """Map an observed provider error to (category, code).

    Deterministic: keyed on error class, HTTP status, and sanitized message
    content. ``insufficient_quota`` maps to account_configuration even when the
    transport class is ``RateLimitError``.
    """
    message = (message_sanitized or "").lower()
    error_type = error_type or ""

    if "insufficient_quota" in message or "credit_balance" in message or "no credits remaining" in message:
        return ACCOUNT_CONFIGURATION, "insufficient_quota"
    if "missing api key" in message or error_type == "ProviderUnavailableError":
        return ACCOUNT_CONFIGURATION, "missing_api_key"
    if "invalid api key" in message or "incorrect api key" in message or "authentication" in message:
        return ACCOUNT_CONFIGURATION, "invalid_api_key"
    if "account disabled" in message or "deactivated" in message:
        return ACCOUNT_CONFIGURATION, "account_disabled"
    if error_type in ("BadRequestError", "UnprocessableEntityError"):
        return ACCOUNT_CONFIGURATION, "invalid_account_configuration"
    if error_type == "RateLimitError":
        return PROVIDER_TRANSPORT, "provider_rate_limit"
    if error_type in ("InternalServerError", "ServiceUnavailableError"):
        return PROVIDER_TRANSPORT, "provider_5xx"
    if error_type == "APITimeoutError":
        return PROVIDER_TRANSPORT, "provider_timeout"
    if error_type in ("APIConnectionError",):
        return PROVIDER_TRANSPORT, "provider_connection_error"
    if http_status is not None and 500 <= http_status < 600:
        return PROVIDER_TRANSPORT, "provider_5xx"
    if http_status is not None and 400 <= http_status < 500:
        return ACCOUNT_CONFIGURATION, "invalid_account_configuration"
    return UNKNOWN, UNCLASSIFIED_CODE


def is_account_incident(category: ErrorCategory | None) -> bool:
    return category == ACCOUNT_CONFIGURATION


def is_transport_incident(category: ErrorCategory | None) -> bool:
    return category == PROVIDER_TRANSPORT
