"""llama.cpp local HTTP server adapter (first live adapter).

Binds only to an explicitly configured loopback endpoint. Inherited HTTP
proxies are disabled and redirects are refused so requests cannot silently
route elsewhere. There is no remote fallback and no API key.

Milestone 1 wires the adapter with honest availability semantics; live
local-model tests stay separate from fake-adapter tests and are marked
NOT RUN/BLOCKED when no server is configured. No weights are downloaded.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .base import AdapterError, AdapterUnavailable, ModelAdapter

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LlamaCppAdapter(ModelAdapter):
    """Talks to a user-started llama.cpp server on loopback."""

    name = "llama.cpp"

    def __init__(
        self,
        endpoint: str,
        model: str = "unknown",
        max_tokens: int = 2048,
        timeout_s: int = 120,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or (parsed.hostname or "") not in ALLOWED_HOSTS:
            raise AdapterError(f"refusing non-loopback endpoint {endpoint!r}")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def _client(self) -> httpx.Client:
        return httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout_s)

    def generate(self, briefing: str, step: int) -> str:
        """One bounded chat completion; the raw content is returned as-is."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": briefing}],
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            with self._client() as client:
                response = client.post(f"{self.endpoint}/v1/chat/completions", json=payload)
        except (httpx.HTTPError, OSError) as exc:
            raise AdapterError(f"local model unreachable: {exc}") from exc
        if response.status_code in (301, 302, 303, 307, 308):
            raise AdapterError("redirect refused: local endpoint must not redirect")
        if response.status_code != 200:
            raise AdapterError(f"local model HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"unexpected local model response shape: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise AdapterError("empty completion from local model")
        return content

    def probe(self) -> None:
        """Capability probe: one tiny real request, parsed as an action.

        Tests actual generation, not merely HTTP 200. Raises AdapterError
        with the precise failure; callers mark live tests BLOCKED/NOT RUN.
        """
        from ..protocol import parse_raw

        raw = self.generate('{"probe": true}. Reply with exactly: {"action":"done",'
                            '"status":"blocked","reason_code":"probe","summary":"probe"}', 0)
        try:
            parse_raw(raw)
        except Exception as exc:
            raise AdapterError(f"probe generation did not yield a valid action: {exc}") from exc

    def provenance(self) -> dict[str, str]:
        """Recorded runtime/model identity; unknown metadata stays unknown."""
        return {
            "adapter": "llama.cpp",
            "endpoint": self.endpoint,
            "model": self.model,
            "note": "live local inference; no remote fallback",
        }


def check_configured(url: str | None) -> str:
    """Return the URL or raise honest unavailability for live tests."""
    if not url:
        raise AdapterUnavailable("no local model configured (set LOCAL_CODER_LLAMA_URL); live test NOT RUN")
    return url
