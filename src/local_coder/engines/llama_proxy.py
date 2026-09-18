"""Loopback proxy between OpenCode and the owned llama.cpp server.

This is the component that bounds tool results and checks the complete
assembled request before any generation is dispatched.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .context_budget import (
    ConservativeEstimator,
    ContextBudgetError,
    ContextPolicy,
    LlamaCppTokenizer,
    TokenCounter,
    prepare_chat_request,
    reject_oversized_write,
)
from .observations import ObservationStore


class ProxyError(Exception):
    """The proxy could not start or is not owned."""


class LlamaProxy:
    """Owned 127.0.0.1 server. OpenCode's baseURL points here."""

    def __init__(
        self,
        upstream: str,
        *,
        expected_model: str,
        store: ObservationStore | None = None,
        policy: ContextPolicy | None = None,
        counter: TokenCounter | None = None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.expected_model = expected_model
        self.store = store or ObservationStore()
        self.policy = policy or ContextPolicy()
        self.counter = counter or _counter_for(self.upstream)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.last_accounting: dict[str, Any] | None = None
        self.dispatched = 0
        self.rejected = 0

    @property
    def url(self) -> str:
        if self.httpd is None:
            raise ProxyError("proxy is not running")
        host, port = self.httpd.server_address[:2]
        host_s = host.decode("ascii") if isinstance(host, (bytes, bytearray)) else str(host)
        return f"http://{host_s}:{int(port)}"

    def start(self) -> dict[str, Any]:
        """Bind an ephemeral loopback port and serve in a daemon thread."""
        handler = _handler_for(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return {"url": self.url, "upstream": self.upstream, "model": self.expected_model}

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.httpd = None
        self.thread = None


def _counter_for(upstream: str) -> TokenCounter:
    try:
        with httpx.Client(trust_env=False, timeout=1.0) as client:
            response = client.get(f"{upstream}/health")
        if response.status_code == 200:
            return LlamaCppTokenizer(upstream)
    except (httpx.HTTPError, OSError):
        pass
    return ConservativeEstimator()


def _handler_for(proxy: LlamaProxy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path.startswith("/local-coder/observe/"):
                self._observe(parsed)
                return
            if parsed.path == "/local-coder/accounting":
                self._json(200, proxy.last_accounting or {})
                return
            self._forward()

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path.endswith("/chat/completions") or parsed.path == "/v1/chat/completions":
                self._chat()
                return
            self._forward()

        def _chat(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._json(400, {"error": {"message": "chat body was not JSON"}})
                return
            want_stream = bool(body.get("stream"))
            try:
                prepared, accounting = prepare_chat_request(
                    body,
                    store=proxy.store,
                    counter=proxy.counter,
                    policy=proxy.policy,
                    expected_model=proxy.expected_model,
                )
            except ContextBudgetError as exc:
                proxy.rejected += 1
                proxy.last_accounting = exc.accounting.__dict__
                self._json(400, {
                    "error": {
                        "code": 400,
                        "type": "context_budget_error",
                        "message": exc.detail,
                        "n_prompt_tokens": exc.accounting.input_tokens,
                        "n_ctx": exc.accounting.n_ctx,
                    }
                })
                return
            proxy.last_accounting = accounting.__dict__
            prepared["stream"] = False
            try:
                with httpx.Client(trust_env=False, timeout=600.0) as client:
                    response = client.post(
                        f"{proxy.upstream}/v1/chat/completions",
                        json=prepared,
                        headers={"Content-Type": "application/json"},
                    )
            except (httpx.HTTPError, OSError) as exc:
                self._json(502, {"error": {"message": f"upstream failed: {exc}"}})
                return
            proxy.dispatched += 1
            try:
                completion = response.json()
            except ValueError:
                self._bytes(response.status_code, response.content, response.headers.get("content-type"))
                return
            completion = reject_oversized_write(completion, proxy.policy)
            if want_stream:
                self._sse(completion)
                return
            self._json(response.status_code, completion)

        def _observe(self, parsed: Any) -> None:
            oid = parsed.path.rsplit("/", 1)[-1]
            query = parse_qs(parsed.query)
            offset = int((query.get("offset") or ["0"])[0])
            limit = query.get("limit")
            try:
                view = proxy.store.read_slice(
                    oid,
                    offset=offset,
                    limit=int(limit[0]) if limit else None,
                )
            except (OSError, ValueError, KeyError) as exc:
                self._json(404, {"error": {"message": str(exc)}})
                return
            self._json(200, {
                "observation_id": view.observation_id,
                "excerpt": view.excerpt,
                "omitted": view.omitted,
                "offset": view.offset,
                "next_offset": view.next_offset,
                "text": view.as_text(),
            })

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            url = f"{proxy.upstream}{self.path}"
            try:
                with httpx.Client(trust_env=False, timeout=60.0) as client:
                    response = client.request(self.command, url, content=raw, headers=_hop(self.headers))
            except (httpx.HTTPError, OSError) as exc:
                self._json(502, {"error": {"message": f"upstream failed: {exc}"}})
                return
            self._bytes(response.status_code, response.content, response.headers.get("content-type"))

        def _json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            self._bytes(status, data, "application/json")

        def _sse(self, completion: dict[str, Any]) -> None:
            chunk = json.dumps(completion)
            body = f"data: {chunk}\n\ndata: [DONE]\n\n".encode()
            self._bytes(200, body, "text/event-stream")

        def _bytes(self, status: int, body: bytes, content_type: str | None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _hop(headers: Any) -> dict[str, str]:
    skip = {"host", "content-length", "connection", "transfer-encoding"}
    return {k: v for k, v in headers.items() if k.lower() not in skip}
