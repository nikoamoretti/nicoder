"""llama.cpp local HTTP server adapter (first live inference adapter).

Binds only to an explicitly configured loopback endpoint. Inherited HTTP
proxies are disabled and redirects are refused so requests cannot silently
route elsewhere. There is no remote fallback, no API key, no hidden retry,
and no second model. Every response is re-validated by the controller;
HTTP 200 never proves the JSON constraint worked — only a parsed action
does.

Endpoint shapes follow the llama.cpp server's official OpenAI-compatible
API (/v1/chat/completions with response_format json_object, /tokenize,
/props, /health, /v1/models). Anything the server does not actually return
stays "unknown" in provenance. Live tests stay separate from fake-adapter
tests and are marked NOT RUN/BLOCKED when no server is configured.
"""
from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..telemetry import llama_server_version, model_file_sha
from .base import AdapterError, AdapterUnavailable, ModelAdapter
from .tokenizer import LlamaCppCounter, TokenCounter

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LlamaCppAdapter(ModelAdapter):
    """Talks to a user-started llama.cpp server on loopback."""

    name = "llama.cpp"

    def __init__(
        self,
        endpoint: str,
        model: str = "unknown",
        max_tokens: int = 2048,
        timeout_s: int = 300,
        model_file: str | None = None,
        quantization: str = "unknown",
        n_ctx: int | None = None,
        temperature: float = 0.2,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or (parsed.hostname or "") not in ALLOWED_HOSTS:
            raise AdapterError(f"refusing non-loopback endpoint {endpoint!r}")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.model_file = model_file
        self.quantization = quantization
        self.n_ctx = n_ctx
        self.temperature = temperature
        self.last_usage: dict | None = None
        self.server_props: dict = {}
        self.local_binary_version = llama_server_version()

    def _client(self) -> httpx.Client:
        return httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout_s)

    def close(self) -> None:
        """Release client resources (explicit cancellation path)."""

    # -- measurement ----------------------------------------------------

    def token_counter(self) -> TokenCounter:
        """Exact counts from the loaded model's own tokenizer."""
        return LlamaCppCounter(self.count_tokens)

    def count_tokens(self, text: str) -> int:
        """POST /tokenize and return the measured token count."""
        try:
            with self._client() as client:
                response = client.post(f"{self.endpoint}/tokenize", json={"content": text})
        except (httpx.HTTPError, OSError) as exc:
            raise AdapterError(f"tokenize unreachable: {exc}") from exc
        if response.status_code != 200:
            raise AdapterError(f"tokenize HTTP {response.status_code}")
        try:
            tokens = response.json()["tokens"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(f"unexpected tokenize shape: {exc}") from exc
        if not isinstance(tokens, list):
            raise AdapterError("unexpected tokenize shape: tokens is not a list")
        return len(tokens)

    def context_limit(self) -> int | None:
        """Configured context size, or None when unverified."""
        return self.n_ctx

    def server_info(self) -> dict:
        """Fetch /props once (metadata only, never a generation)."""
        if self.server_props:
            return self.server_props
        try:
            with self._client() as client:
                response = client.get(f"{self.endpoint}/props", timeout=15)
            if response.status_code == 200:
                self.server_props = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            self.server_props = {}
        return self.server_props if isinstance(self.server_props, dict) else {}

    # -- generation -----------------------------------------------------

    def generate(self, briefing: str, step: int) -> str:
        """One bounded JSON-constrained chat completion; raw content as-is."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": briefing}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        self.last_usage = None
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
            finish = data["choices"][0].get("finish_reason", "unknown")
            usage = data.get("usage", {})
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"unexpected local model response shape: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise AdapterError("empty completion from local model")
        self.last_usage = {
            "finish_reason": str(finish),
            "prompt_tokens": _as_int(usage.get("prompt_tokens")),
            "completion_tokens": _as_int(usage.get("completion_tokens")),
        }
        return content

    # -- capability probe -------------------------------------------------

    def probe(self, save_dir: str | Path | None = None) -> dict:
        """Verify real capabilities, not merely HTTP 200.

        Checks generation with the JSON constraint honored (response parses
        as JSON and as a valid action), /tokenize measurement, and records
        server props. Probe records live outside episodes and are never
        counted as generation attempts. Raises AdapterError with specifics.
        """
        from ..protocol import parse_raw

        record: dict = {"endpoint": self.endpoint, "model": self.model, "checks": {}}
        try:
            with self._client() as client:
                health = client.get(f"{self.endpoint}/health")
                record["checks"]["health"] = str(health.status_code)
                try:
                    self.server_props = health.json() if health.status_code == 200 else {}
                except ValueError:
                    self.server_props = {}
                props = client.get(f"{self.endpoint}/props")
                if props.status_code == 200:
                    try:
                        self.server_props = props.json()
                    except ValueError:
                        pass
                record["checks"]["props"] = str(props.status_code)
        except (httpx.HTTPError, OSError) as exc:
            raise AdapterError(f"probe: server unreachable: {exc}") from exc
        raw = self.generate(
            'Reply with exactly this JSON object and nothing else: {"action":"done",'
            '"status":"blocked","reason_code":"probe","summary":"probe"}', 0)
        try:
            parsed = json.loads(raw)
            record["checks"]["json_constraint"] = "honored" if isinstance(parsed, dict) else "violated"
        except ValueError as exc:
            raise AdapterError(f"probe: JSON constraint violated: {exc}") from exc
        try:
            parse_raw(raw)
            record["checks"]["action_parse"] = "ok"
        except Exception as exc:
            raise AdapterError(f"probe generation did not yield a valid action: {exc}") from exc
        try:
            measured = self.count_tokens("probe")
            record["checks"]["tokenize"] = f"ok ({measured} tokens)"
        except AdapterError as exc:
            raise AdapterError(f"probe: tokenize failed: {exc}") from exc
        record["usage"] = self.last_usage
        record["server_props"] = self.server_props
        if save_dir is not None:
            from datetime import datetime

            dest = Path(save_dir) / "_probes"
            dest.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            (dest / f"probe-{stamp}.json").write_text(json.dumps(record, indent=2))
        return record

    def provenance(self) -> dict[str, str]:
        """Recorded runtime/model identity; unknown metadata stays unknown."""
        ftype = self.server_props.get("model_ftype", "unknown")
        quantization = self.quantization if self.quantization != "unknown" else str(ftype)
        build = self.server_props.get("build_info", "unknown")
        template = "present" if self.server_props.get("chat_template") else (
            "unknown" if "chat_template" not in self.server_props else "absent")
        return {
            "adapter": "llama.cpp",
            "endpoint": self.endpoint,
            "model": self.model,
            "model_file": self.model_file or "unknown",
            "model_sha256": model_file_sha(self.model_file),
            "quantization": str(quantization),
            "n_ctx": str(self.n_ctx) if self.n_ctx else "unknown",
            "n_ctx_source": "configured" if self.n_ctx else "unknown",
            "chat_template": template,
            "server_build": str(build),
            "server_slots": str(self.server_props.get("total_slots", "unknown")),
            "local_binary_version": self.local_binary_version,
            "note": "live local inference; no remote fallback",
        }


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def check_configured(url: str | None) -> str:
    """Return the URL or raise honest unavailability for live tests."""
    if not url:
        raise AdapterUnavailable("no local model configured (set LOCAL_CODER_LLAMA_URL); live test NOT RUN")
    return url
