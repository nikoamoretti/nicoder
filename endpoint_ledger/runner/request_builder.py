"""Logical request payload builder shared by providers and persistence.

The stored "raw_request" is the logical request (messages, parameters, tools).
It intentionally contains no authorization headers, keys, or transport detail.
"""

from __future__ import annotations

from typing import Any

from endpoint_ledger.db.models import ProbeVersion, ProviderEndpoint
from endpoint_ledger.sanitize import redact_secrets


def build_request_payload(
    endpoint_config: ProviderEndpoint,
    probe_version: ProbeVersion,
) -> dict[str, Any]:
    messages = [
        {"role": m.get("role", "user"), "content": m.get("content")}
        for m in probe_version.prompt_messages
    ]
    params = dict(endpoint_config.request_defaults.get("request") or {})
    params.update(probe_version.request_parameters or {})
    payload: dict[str, Any] = {"messages": messages, "request_parameters": params}
    if probe_version.tools:
        payload["tools"] = probe_version.tools
    # Defense in depth: never persist credentials even if misconfigured upstream.
    return redact_secrets(payload)  # type: ignore[return-value]
