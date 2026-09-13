"""llama.cpp local HTTP server adapter (first live inference adapter).

Binds only to an explicitly configured loopback endpoint. Inherited HTTP
proxies are disabled and redirects are refused so requests cannot silently
route elsewhere. There is no remote fallback, no API key, no hidden retry,
and no second model. Every response is re-validated by the controller;
HTTP 200 never proves a constraint worked — only a parsed action does.

Constrained generation sends the action-schema GBNF grammar together with
response_format json_object on the installed build's chat endpoint
(verified: llama.cpp 0.4.0 honors `grammar` at the decoder; json_object
alone does not constrain this model family). The exact constraint sent is
recorded per generation; no global enforcement is ever claimed.

Transport reads streamed SSE with an incremental response byte cap, an
overall deadline, and real cancellation: cancel() closes the active
stream so late bytes can never become episode state. A dispatched request
with an unknown outcome is reported as an error and still consumes its
episode slot (accounted by the controller, never resampled here).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..telemetry import llama_server_version, model_file_sha
from .base import AdapterError, AdapterUnavailable, ModelAdapter
from .grammar import GRAMMARS, constraint_id
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
        constraint: str = constraint_id(),
        channel_policy: str = "legacy",
        thinking: str = "auto",
        response_max_bytes: int = 131072,
        read_timeout_s: int = 120,
    ) -> None:
        if channel_policy not in ("legacy", "final-only"):
            raise AdapterError(f"unknown channel policy {channel_policy!r}")
        if thinking not in ("auto", "off"):
            raise AdapterError(f"unknown thinking mode {thinking!r}")
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
        self.constraint = constraint
        self.channel_policy = channel_policy
        self.thinking = thinking
        self.response_max_bytes = response_max_bytes
        self.read_timeout_s = read_timeout_s
        self.last_usage: dict | None = None
        self.last_request: dict | None = None
        self.server_props: dict = {}
        self.local_binary_version = llama_server_version()
        self._cancel = threading.Event()
        self._active_lock = threading.Lock()
        self._active_response: httpx.Response | None = None

    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(connect=10.0, read=float(self.read_timeout_s),
                                write=10.0, pool=10.0)
        return httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout)

    def cancel(self) -> None:
        """Cancel the in-flight request, if any, and discard late bytes."""
        self._cancel.set()
        with self._active_lock:
            response = self._active_response
        if response is not None:
            try:
                response.close()
            except (httpx.HTTPError, OSError, ValueError):
                pass

    def close(self) -> None:
        """Release activity: cancel any in-flight request."""
        self.cancel()

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

    def _payload(self, briefing: str) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": briefing}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
        }
        grammar_fn = GRAMMARS.get(self.constraint)
        if grammar_fn is not None:
            payload["grammar"] = grammar_fn()
        return payload

    def generate(self, briefing: str, step: int) -> str:
        """One bounded grammar-constrained streamed completion.

        Raises AdapterError on timeout, byte-cap breach, cancellation,
        interruption, or empty output. Either returns the full content or
        raises: partial bytes are never surfaced as a response.
        """
        payload = self._payload(briefing)
        self.last_usage = None
        self.last_request = {"url": f"{self.endpoint}/v1/chat/completions", "payload": payload}
        self._cancel.clear()
        deadline = time.monotonic() + self.timeout_s
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        received = 0
        finish = "unknown"
        usage: dict = {}
        timings: dict = {}
        try:
            with self._client() as client:
                with client.stream("POST", self.last_request["url"], json=payload) as response:
                    with self._active_lock:
                        self._active_response = response
                    try:
                        if response.status_code in (301, 302, 303, 307, 308):
                            raise AdapterError("redirect refused: local endpoint must not redirect")
                        if response.status_code != 200:
                            raise AdapterError(f"local model HTTP {response.status_code}")
                        for line in response.iter_lines():
                            if self._cancel.is_set():
                                raise AdapterError("cancelled by supervisor")
                            if time.monotonic() > deadline:
                                raise AdapterError(f"overall deadline exceeded ({self.timeout_s}s)")
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            received += len(line.encode("utf-8"))
                            if received > self.response_max_bytes:
                                raise AdapterError(
                                    f"response exceeded {self.response_max_bytes} byte cap while receiving")
                            try:
                                chunk = json.loads(data)
                            except ValueError as exc:
                                raise AdapterError(f"malformed stream chunk: {exc}") from exc
                            choices = chunk.get("choices", [])
                            if not choices:
                                if isinstance(chunk.get("usage"), dict):
                                    usage = chunk["usage"]
                                continue
                            delta = choices[0].get("delta", {})
                            for key, bucket in (("content", content_parts),
                                                ("reasoning_content", reasoning_parts)):
                                piece = delta.get(key)
                                if isinstance(piece, str) and piece:
                                    bucket.append(piece)
                            if choices[0].get("finish_reason"):
                                finish = str(choices[0]["finish_reason"])
                            if isinstance(chunk.get("usage"), dict):
                                usage = chunk["usage"]
                            if isinstance(chunk.get("timings"), dict):
                                timings = chunk["timings"]
                    finally:
                        with self._active_lock:
                            self._active_response = None
        except AdapterError:
            self.last_usage = None
            raise
        except httpx.TimeoutException:
            # A stalled stream surfaces here: arbitrate cancel, then the
            # overall deadline, then treat the stall itself as a failure.
            # (Cross-thread close() may not interrupt a blocked socket read
            # promptly, so cancellation latency is bounded by read_timeout_s.)
            self.last_usage = None
            if self._cancel.is_set():
                raise AdapterError("cancelled by supervisor") from None
            if time.monotonic() > deadline:
                raise AdapterError(f"overall deadline exceeded ({self.timeout_s}s)") from None
            raise AdapterError(f"stream stalled over {self.read_timeout_s}s without bytes") from None
        except (httpx.HTTPError, OSError) as exc:
            self.last_usage = None
            raise AdapterError(f"local model transport failed: {exc}") from exc
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        reasoning_present = bool(reasoning.strip())
        if self.channel_policy == "final-only":
            if not content.strip():
                raise AdapterError(
                    "empty final content under final-only channel policy "
                    "(reasoning-only output is a compatibility failure, not an action)")
            channel = "content"
            text = content
        elif content.strip():
            channel = "content"
            text = content
        elif reasoning.strip():
            channel = "reasoning_content"
            text = reasoning
        else:
            raise AdapterError("empty completion from local model")
        self.last_usage = {
            "finish_reason": finish,
            "prompt_tokens": _as_int(usage.get("prompt_tokens")),
            "completion_tokens": _as_int(usage.get("completion_tokens")),
            "channel": channel,
            "channel_policy": self.channel_policy,
            "reasoning_present": reasoning_present,
            "streamed_bytes": received,
            "timings": timings,
        }
        return text

    # -- capability probe -------------------------------------------------

    def probe(self, save_dir: str | Path | None = None) -> dict:
        """Verify real capabilities, not merely HTTP 200.

        Checks grammar-constrained generation (response parses as JSON and
        as a valid action), /tokenize measurement, and records server props.
        When thinking is declared off, the probe additionally requires the
        action in the final-answer channel with no reasoning output: a
        reasoning-only response is a compatibility failure. Probe records
        live outside episodes and are never counted as generation attempts.
        Raises AdapterError with specifics.
        """
        from ..protocol import parse_raw

        record: dict = {"endpoint": self.endpoint, "model": self.model,
                        "constraint": self.constraint,
                        "channel_policy": self.channel_policy,
                        "thinking": getattr(self, "thinking", "auto"),
                        "checks": {}}
        try:
            with self._client() as client:
                health = client.get(f"{self.endpoint}/health")
                record["checks"]["health"] = str(health.status_code)
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
        if self.last_usage and self.last_usage.get("channel"):
            record["channel"] = self.last_usage["channel"]
        if getattr(self, "thinking", "auto") == "off":
            if (self.last_usage or {}).get("reasoning_present"):
                raise AdapterError("probe: thinking=off but the server emitted reasoning output")
            if (self.last_usage or {}).get("channel") != "content":
                raise AdapterError("probe: thinking=off but no final-answer content arrived")
        try:
            parsed = json.loads(raw)
            record["checks"]["constraint"] = "json_parsed" if isinstance(parsed, dict) else "violated"
        except ValueError as exc:
            raise AdapterError(f"probe: constraint produced non-JSON: {exc}") from exc
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
            "constraint": self.constraint,
            "channel_policy": self.channel_policy,
            "thinking": self.thinking,
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
