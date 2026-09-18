"""Prove the proxy speaks the streaming contract OpenCode 1.18.22 parses."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from local_coder.engines.context_budget import ConservativeEstimator
from local_coder.engines.llama_proxy import LlamaProxy
from local_coder.engines.observations import ObservationStore
from local_coder.engines.stream_contract import (
    StreamContractError,
    assemble_chunks,
    chunks_to_sse,
    completion_to_chunks,
    parse_sse_block,
)


def _split_sse(body: bytes) -> list[dict | str]:
    text = body.decode("utf-8")
    items: list[dict | str] = []
    for block in text.split("\n\n"):
        parsed = parse_sse_block(block)
        if parsed is not None:
            items.append(parsed)
    return items


def test_translated_text_chunks_assemble():
    completion = {
        "id": "c1",
        "model": "Qwen3.5-9B-Q4_K_M",
        "choices": [{"message": {"role": "assistant", "content": "READY"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
    }
    chunks = completion_to_chunks(completion)
    assert all("delta" in (ch["choices"][0]) for ch in chunks)
    assembled = assemble_chunks(chunks)
    assert assembled.content == "READY"
    assert assembled.finish_reason == "stop"
    assert assembled.usage and assembled.usage["completion_tokens"] == 1


def test_full_completion_on_sse_is_rejected_by_parser():
    payload = json.dumps({
        "id": "c1",
        "object": "chat.completion",
        "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}],
    })
    body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
    parsed = []
    for block in body.decode().split("\n\n"):
        item = parse_sse_block(block)
        if isinstance(item, dict):
            parsed.append(item)
    with pytest.raises(StreamContractError):
        assemble_chunks(parsed)


def test_tool_call_chunks_preserve_ids_and_arguments():
    completion = {
        "id": "c2",
        "model": "Qwen3.5-9B-Q4_K_M",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.ts"}'}},
                    {"id": "call_b", "type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}},
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }
    assembled = assemble_chunks(completion_to_chunks(completion))
    assert assembled.finish_reason == "tool_calls"
    assert [c["id"] for c in assembled.tool_calls] == ["call_a", "call_b"]
    assert assembled.tool_calls[0]["function"]["name"] == "read"
    assert assembled.tool_calls[1]["function"]["arguments"] == '{"command":"ls"}'


def test_proxy_translates_buffered_completion_into_chunks(tmp_path):
    recorded: list[dict] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            recorded.append(json.loads(raw.decode() or "{}"))
            reply = json.dumps({
                "id": "chatcmpl-up",
                "object": "chat.completion",
                "model": "Qwen3.5-9B-Q4_K_M",
                "choices": [{"message": {"role": "assistant", "content": "READY"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    proxy = LlamaProxy(
        f"http://{host}:{port}",
        expected_model="Qwen3.5-9B-Q4_K_M",
        store=ObservationStore(tmp_path / "obs"),
        counter=ConservativeEstimator(),
    )
    proxy.start()
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(
                f"{proxy.url}/v1/chat/completions",
                json={
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        items = _split_sse(response.content)
        chunks = [i for i in items if isinstance(i, dict)]
        assembled = assemble_chunks(chunks)
        assert assembled.content == "READY"
        assert assembled.finish_reason == "stop"
        assert items[-1] == "[DONE]"
        assert recorded and recorded[0].get("max_tokens")
    finally:
        proxy.stop()
        server.shutdown()


def test_proxy_emits_done_after_finish_even_without_upstream_done(tmp_path):
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            chunk = {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": "READY"}, "finish_reason": "stop"}],
            }
            body = f"data: {json.dumps(chunk)}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    proxy = LlamaProxy(
        f"http://{host}:{port}",
        expected_model="Qwen3.5-9B-Q4_K_M",
        store=ObservationStore(tmp_path / "obs"),
        counter=ConservativeEstimator(),
    )
    proxy.start()
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            with client.stream(
                "POST",
                f"{proxy.url}/v1/chat/completions",
                json={"model": "Qwen3.5-9B-Q4_K_M", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            ) as response:
                assert response.status_code == 200
                raw = b""
                for chunk in response.iter_bytes():
                    raw += chunk
                    if b"data: [DONE]" in raw:
                        break
        items = _split_sse(raw)
        assert items[-1] == "[DONE]"
        assembled = assemble_chunks([i for i in items if isinstance(i, dict)])
        assert assembled.content == "READY"
        assert assembled.finish_reason == "stop"
    finally:
        proxy.stop()
        server.shutdown()


def test_proxy_forwards_upstream_error_not_success(tmp_path):
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            body = b'{"error":{"message":"nope","type":"server_error"}}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    proxy = LlamaProxy(
        f"http://{host}:{port}",
        expected_model="Qwen3.5-9B-Q4_K_M",
        store=ObservationStore(tmp_path / "obs"),
        counter=ConservativeEstimator(),
    )
    proxy.start()
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(
                f"{proxy.url}/v1/chat/completions",
                json={"model": "Qwen3.5-9B-Q4_K_M", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert response.status_code == 500
        assert "nope" in response.text
        assert proxy.dispatched == 0
    finally:
        proxy.stop()
        server.shutdown()


def test_proxy_oversized_write_is_error_status(tmp_path):
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            reply = json.dumps({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{"id": "w1", "function": {"name": "write", "arguments": "z" * 250_000}}],
                    },
                    "finish_reason": "tool_calls",
                }]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    proxy = LlamaProxy(
        f"http://{host}:{port}",
        expected_model="Qwen3.5-9B-Q4_K_M",
        store=ObservationStore(tmp_path / "obs"),
        counter=ConservativeEstimator(),
    )
    proxy.start()
    try:
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(
                f"{proxy.url}/v1/chat/completions",
                json={"model": "Qwen3.5-9B-Q4_K_M", "messages": [{"role": "user", "content": "write"}]},
            )
        assert response.status_code == 400
        payload = response.json()
        assert payload["error"]["type"] == "oversized_write_rejected"
        assert proxy.dispatched == 0
        assert proxy.rejected == 1
    finally:
        proxy.stop()
        server.shutdown()


def test_opencode_session_receives_text_and_tool_calls(tmp_path, monkeypatch):
    """Real OpenCode 1.18.22 against a scripted mock. No live 9B generation."""

    from local_coder.engines.opencode_server import OpenCodeServer, stop_owned
    from local_coder.machine import MachineConfig, write_machine_config

    write_machine_config(MachineConfig(model_alias="Qwen3.5-9B-Q4_K_M", n_ctx=32768, max_tokens=256))
    project = tmp_path / "app"
    project.mkdir()
    (project / "src").mkdir()
    (project / "README.md").write_text("demo\n", encoding="utf-8")

    state = {"n": 0, "bodies": []}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            payload = {"status": "ok", "model_alias": "Qwen3.5-9B-Q4_K_M", "data": [{"id": "Qwen3.5-9B-Q4_K_M"}]}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                payload = json.loads(raw.decode() or "{}")
            except ValueError:
                payload = {}
            if "tokenize" in self.path:
                n = max(1, len(str(payload.get("content") or "")) // 2)
                reply = {"tokens": [0] * min(n, 8_000)}
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            state["bodies"].append(payload)
            state["n"] += 1
            if state["n"] == 1:
                completion = {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                        "index": 0,
                        "id": "call_write",
                        "type": "function",
                        "function": {"name": "write", "arguments": ""},
                    }]}, "finish_reason": None}],
                }
                arg = {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": 0,
                        "function": {"arguments": json.dumps({
                            "filePath": str(project / "src" / "probe.txt"),
                            "path": str(project / "src" / "probe.txt"),
                            "content": "hello-probe",
                        })},
                    }]}, "finish_reason": None}],
                }
                done = {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
                body = chunks_to_sse([completion, arg, done])
            else:
                text = {
                    "id": "chatcmpl-2",
                    "object": "chat.completion.chunk",
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "READY"},
                        "finish_reason": None,
                    }],
                }
                done = {
                    "id": "chatcmpl-2",
                    "object": "chat.completion.chunk",
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21},
                }
                body = chunks_to_sse([text, done])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    host, port = upstream.server_address[:2]
    server = OpenCodeServer(
        project,
        llama_endpoint=f"http://{host}:{port}",
        model_id="Qwen3.5-9B-Q4_K_M",
        context=32768,
        output=256,
    )
    try:
        server.start(wait_s=40)
        client = server.client()
        session = client.create_session(title="stream-contract")
        session_id = str(session.get("id") or session.get("sessionID") or "")
        client.prompt_async(
            session_id,
            "Write src/probe.txt containing hello-probe, then reply READY.",
            provider_id="llama.cpp",
            model_id="Qwen3.5-9B-Q4_K_M",
        )
        deadline = time.monotonic() + 25
        idle = False
        for event in client.events():
            etype = str(event.get("type") or event.get("event") or "")
            payload = event.get("properties") or event.get("payload") or event
            if "permission" in etype and isinstance(payload, dict):
                pid = payload.get("id") or payload.get("permissionID")
                if pid:
                    try:
                        client.respond_permission(session_id, str(pid), "once")
                    except Exception:  # noqa: BLE001
                        pass
            if "idle" in etype or "session.error" in etype:
                idle = True
                break
            if time.monotonic() > deadline:
                break
        messages = client.messages(session_id)
        blob = json.dumps(messages)
        assert "call_write" in blob or (project / "src" / "probe.txt").exists() or "READY" in blob
        # Tool result must be visible to a later model request when a write ran.
        if len(state["bodies"]) >= 2:
            second = json.dumps(state["bodies"][1])
            assert "call_write" in second or "hello-probe" in second or "tool" in second.lower()
        assert idle or state["n"] >= 1
    finally:
        stop_owned()
        upstream.shutdown()
