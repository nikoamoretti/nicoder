"""Secret redaction and sanitization.

Used everywhere an exception message or external value can reach persistence,
logs, or the web UI. Never log or store authorization headers or API keys.
"""

from __future__ import annotations

import os
import re
from typing import Any

_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_BEARER_PATTERN = re.compile(r"(?i)(authorization|api[-_]?key)\s*[:=]\s*\S+")
_REDACTED = "[REDACTED]"


def _secret_values() -> list[str]:
    """Values that must always be redacted if they leak into text."""
    values = []
    for key in ("OPENAI_API_KEY", "API_KEY", "AUTH_TOKEN"):
        value = os.getenv(key)
        if value:
            values.append(value)
    return values


def sanitize_message(message: str | None) -> str:
    """Redact secrets from an exception message or other free text."""
    if not message:
        return ""
    redacted = message
    for value in _secret_values():
        if value and value in redacted:
            redacted = redacted.replace(value, _REDACTED)
    redacted = _SK_PATTERN.sub(_REDACTED, redacted)
    redacted = _BEARER_PATTERN.sub(rf"\1={_REDACTED}", redacted)
    return redacted


def redact_secrets(obj: Any) -> Any:
    """Recursively redact secret-looking values inside JSON-able structures."""
    if isinstance(obj, dict):
        redacted: dict[str, Any] = {}
        for key, value in obj.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if "authorization" in normalized or "apikey" in normalized:
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_secrets(value)
        return redacted
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    if isinstance(obj, str):
        return sanitize_message(obj)
    return obj
