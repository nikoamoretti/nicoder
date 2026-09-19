"""Loopback proxy between OpenCode and the owned llama.cpp server.

Bounds tool results, checks the complete request, then forwards using the
OpenAI-compatible streaming contract OpenCode 1.18.22 actually parses.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .context_budget import (
    WRITE_TOOLS,
    ConservativeEstimator,
    ContextBudgetError,
    ContextPolicy,
    LlamaCppTokenizer,
    TokenCounter,
    gate_completion,
    prepare_chat_request,
)
from .observations import ObservationStore
from .stream_contract import (
    StreamContractError,
    ToolCallAccumulator,
    chunks_to_sse,
    completion_to_chunks,
    error_to_sse,
    looks_like_chunk,
    parse_sse_block,
)

UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


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
        read_timeout: float = 180.0,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.expected_model = expected_model
        self.store = store or ObservationStore()
        self.policy = policy or ContextPolicy()
        self.counter = counter or _counter_for(self.upstream)
        self.read_timeout = read_timeout
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.last_accounting: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.dispatched = 0
        self.rejected = 0
        self.cancelled = 0
        self.active_upstreams: list[httpx.Response] = []
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        if self.httpd is None:
            raise ProxyError("proxy is not running")
        host, port = self.httpd.server_address[:2]
        host_s = host.decode("ascii") if isinstance(host, (bytes, bytearray)) else str(host)
        return f"http://{host_s}:{int(port)}"

    def start(self, *, port: int = 0) -> dict[str, Any]:
        """Bind a loopback port (0 = ephemeral) and serve in a daemon thread."""
        handler = _handler_for(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return {"url": self.url, "upstream": self.upstream, "model": self.expected_model}

    def stop(self) -> None:
        with self._lock:
            for response in list(self.active_upstreams):
                try:
                    response.close()
                except Exception:  # noqa: BLE001 — best-effort cancel
                    pass
            self.active_upstreams.clear()
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
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def handle(self) -> None:
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                proxy.cancelled += 1

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/local-coder/observe/"):
                self._observe(parsed)
                return
            if parsed.path == "/local-coder/accounting":
                self._json(200, proxy.last_accounting or {})
                return
            if parsed.path == "/local-coder/history":
                self._json(200, {
                    "dispatched": proxy.dispatched,
                    "rejected": proxy.rejected,
                    "cancelled": proxy.cancelled,
                    "history": proxy.history,
                })
                return
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
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
                self._error(400, {
                    "type": "context_budget_error",
                    "code": 400,
                    "message": exc.detail,
                    "n_prompt_tokens": exc.accounting.input_tokens,
                    "n_ctx": exc.accounting.n_ctx,
                }, want_stream=False)
                return
            proxy.last_accounting = accounting.__dict__
            proxy.history.append(dict(accounting.__dict__))
            timeout = httpx.Timeout(
                connect=10.0, read=proxy.read_timeout, write=30.0, pool=10.0,
            )
            if want_stream:
                self._chat_stream(prepared, timeout)
                return
            self._chat_json(prepared, timeout)

        def _chat_json(self, prepared: dict[str, Any], timeout: httpx.Timeout) -> None:
            prepared["stream"] = False
            try:
                with httpx.Client(trust_env=False, timeout=timeout) as client:
                    response = client.post(
                        f"{proxy.upstream}/v1/chat/completions",
                        json=prepared,
                        headers={"Content-Type": "application/json"},
                    )
            except (httpx.HTTPError, OSError) as exc:
                self._error(502, {"type": "upstream_error", "message": f"upstream failed: {exc}"})
                return
            if response.status_code != 200:
                self._bytes(response.status_code, response.content, response.headers.get("content-type"))
                return
            try:
                completion = response.json()
            except ValueError:
                self._error(502, {"type": "upstream_error", "message": "upstream returned non-JSON"})
                return
            gate = gate_completion(completion, proxy.policy, proxy.store)
            if not gate.allowed:
                proxy.rejected += 1
                self._error(400, gate.error or {"message": "rejected"})
                return
            proxy.dispatched += 1
            self._json(200, gate.completion)

        def _chat_stream(self, prepared: dict[str, Any], timeout: httpx.Timeout) -> None:
            prepared["stream"] = True
            prepared.setdefault("stream_options", {"include_usage": True})
            client = httpx.Client(trust_env=False, timeout=timeout)
            try:
                request = client.build_request(
                    "POST",
                    f"{proxy.upstream}/v1/chat/completions",
                    json=prepared,
                    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                )
                response = client.send(request, stream=True)
            except (httpx.HTTPError, OSError) as exc:
                client.close()
                self._error(502, {"type": "upstream_error", "message": f"upstream failed: {exc}"})
                return
            with proxy._lock:
                proxy.active_upstreams.append(response)
            try:
                if response.status_code != 200:
                    body = response.read()
                    self._bytes(response.status_code, body, response.headers.get("content-type"))
                    return
                ctype = (response.headers.get("content-type") or "").lower()
                if "text/event-stream" not in ctype:
                    raw = response.read()
                    try:
                        completion = json.loads(raw.decode("utf-8") or "{}")
                    except ValueError:
                        self._error(502, {"type": "upstream_error", "message": "upstream returned non-JSON"})
                        return
                    if response.status_code != 200 or completion.get("error"):
                        err = completion.get("error") if isinstance(completion, dict) else None
                        self._error(
                            response.status_code if response.status_code != 200 else 502,
                            err if isinstance(err, dict) else {"message": raw[:300].decode("utf-8", "replace")},
                        )
                        return
                    self._emit_translated(completion)
                    return
                self._pipe_sse(response)
            except (BrokenPipeError, ConnectionResetError, httpx.HTTPError):
                proxy.cancelled += 1
            finally:
                with proxy._lock:
                    if response in proxy.active_upstreams:
                        proxy.active_upstreams.remove(response)
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass
                client.close()

        def _pipe_sse(self, response: httpx.Response) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            acc = ToolCallAccumulator()
            buffer = ""
            sent = False
            try:
                for piece in response.iter_text():
                    buffer += piece
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        parsed = parse_sse_block(block)
                        if parsed is None:
                            continue
                        if parsed == "[DONE]":
                            if not sent:
                                self.wfile.write(error_to_sse({
                                    "type": "truncated_stream",
                                    "message": "upstream ended before a complete reply",
                                }))
                                self.wfile.flush()
                                return
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                            return
                        if not isinstance(parsed, dict):
                            continue
                        if parsed.get("error"):
                            self.wfile.write(error_to_sse(
                                parsed["error"] if isinstance(parsed["error"], dict)
                                else {"message": str(parsed["error"])}
                            ))
                            self.wfile.flush()
                            return
                        if not looks_like_chunk(parsed):
                            if parsed.get("object") == "chat.completion" or parsed.get("choices"):
                                raise StreamContractError("full completion on a stream")
                            continue
                        acc.ingest(parsed)
                        oversized = _oversized_from_acc(acc, proxy.policy)
                        if oversized is not None:
                            proxy.rejected += 1
                            self.wfile.write(error_to_sse(oversized))
                            self.wfile.flush()
                            return
                        self.wfile.write(f"data: {json.dumps(parsed, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                        sent = True
                        if _finished(parsed):
                            proxy.dispatched += 1
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                            return
                if sent:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                self.wfile.write(error_to_sse({
                    "type": "truncated_stream",
                    "message": "upstream ended before a complete reply",
                }))
                self.wfile.flush()
            except StreamContractError as exc:
                if sent:
                    self.wfile.write(error_to_sse({"type": "stream_contract_error", "message": exc.detail}))
                    self.wfile.flush()
                else:
                    raise
            except (BrokenPipeError, ConnectionResetError):
                proxy.cancelled += 1
                return

        def _emit_translated(self, completion: dict[str, Any]) -> None:
            gate = gate_completion(completion, proxy.policy, proxy.store)
            if not gate.allowed:
                proxy.rejected += 1
                self._error(400, gate.error or {"message": "rejected"}, want_stream=True)
                return
            assert gate.completion is not None
            try:
                chunks = completion_to_chunks(gate.completion)
            except StreamContractError as exc:
                self._error(502, {"type": "stream_contract_error", "message": exc.detail}, want_stream=True)
                return
            proxy.dispatched += 1
            body = chunks_to_sse(chunks)
            self._bytes(200, body, "text/event-stream")

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
                self._error(502, {"type": "upstream_error", "message": f"upstream failed: {exc}"})
                return
            self._bytes(response.status_code, response.content, response.headers.get("content-type"))

        def _error(self, status: int, error: dict[str, Any], *, want_stream: bool = False) -> None:
            if want_stream:
                self._bytes(status, error_to_sse(error), "text/event-stream")
                return
            self._json(status, {"error": error})

        def _json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            self._bytes(status, data, "application/json")

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


def _finished(chunk: dict[str, Any]) -> bool:
    for choice in chunk.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return True
    return False


def _oversized_from_acc(acc: ToolCallAccumulator, policy: ContextPolicy) -> dict[str, Any] | None:
    for slot in acc.calls.values():
        name = str(slot.get("name") or "")
        args = str(slot.get("arguments") or "")
        if name in WRITE_TOOLS and len(args) > policy.clip.write_arg_chars:
            return {
                "type": "oversized_write_rejected",
                "code": "oversized_write",
                "message": (
                    f"Rejected oversized {name} arguments "
                    f"({len(args)} > {policy.clip.write_arg_chars}). "
                    "No partial write was applied."
                ),
                "tool": name,
                "tool_call_id": slot.get("id"),
                "argument_chars": len(args),
                "limit_chars": policy.clip.write_arg_chars,
            }
    return None
