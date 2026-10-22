"""The GPU swapper with a fake engine: listing, loading, swapping, fallbacks, streaming and errors."""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from local_coder.gpu.manifest import dumps
from local_coder.gpu.swapper import Swapper, make_server

FAKE_ENGINE = r'''
import argparse, json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--model")
parser.add_argument("--delay", type=float, default=0.3)
parser.add_argument("--fail", action="store_true")
args = parser.parse_args()
if args.fail:
    print("unsupported flag", flush=True)
    sys.exit(3)
started = time.monotonic()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        ready = time.monotonic() - started > args.delay
        self.send_response(200 if ready else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = f"hi from {args.model}"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"model": args.model, "choices": [{"index": 0, "delta": {"content": text}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            done = {"model": args.model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
            self.wfile.flush()
            self.close_connection = True
        else:
            reply = json.dumps({"model": args.model, "choices": [{"message": {"content": text}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)
ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def gpu(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    library = {"version": 1, "models": [
        {"id": "alpha", "name": "Alpha", "dir": "alpha", "engine": "vllm", "args": ["--delay", "0.3"]},
        {"id": "beta", "name": "Beta", "dir": "beta", "engine": "vllm", "args": ["--fail"],
         "fallback_args": ["--delay", "0.2"]},
        {"id": "broken", "name": "Broken", "dir": "broken", "engine": "vllm", "args": ["--fail"]},
        {"id": "slow", "name": "Slow", "dir": "slow", "engine": "vllm", "args": ["--delay", "1.2"]},
    ]}
    (root / "models.json").write_text(dumps(library), encoding="utf-8")
    script = tmp_path / "fake_engine.py"
    script.write_text(FAKE_ENGINE, encoding="utf-8")
    launched: list[list[str]] = []

    def launcher(command: list[str]) -> subprocess.Popen[bytes]:
        launched.append(command)
        port = command[command.index("--port") + 1]
        model = command[command.index("--served-model-name") + 1]
        extra = command[command.index("--port") + 2:]
        return subprocess.Popen([sys.executable, str(script), "--port", port, "--model", model, *extra],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

    swapper = Swapper(root, engine_port=_free_port(), load_timeout=20, drain_timeout=5, launcher=launcher)
    port = _free_port()
    server = make_server(swapper, port, host="127.0.0.1")
    server.RequestHandlerClass.keepalive_s = 0.25  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield swapper, port, launched
    server.shutdown()
    swapper.stop()


def _post(port: int, path: str, payload: dict) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
    response = conn.getresponse()
    return response.status, response.read().decode()


def _get(port: int, path: str) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    return json.loads(conn.getresponse().read())


def _events(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines()
            if line.startswith("data:") and line.strip() != "data: [DONE]"]


def test_lists_the_library_without_starting_anything(gpu):
    swapper, port, launched = gpu
    listed = _get(port, "/v1/models")
    assert [row["id"] for row in listed["data"]] == ["alpha", "beta", "broken", "slow"]
    assert _get(port, "/health")["status"] == "idle"
    assert launched == []


def test_streaming_request_loads_the_model_and_proxies_it(gpu):
    swapper, port, launched = gpu
    status, body = _post(port, "/v1/chat/completions", {"model": "alpha", "stream": True, "messages": []})
    assert status == 200
    assert _events(body)[0]["choices"][0]["delta"]["content"] == "hi from alpha"
    assert body.rstrip().endswith("data: [DONE]")
    assert swapper.active == "alpha" and len(launched) == 1
    assert launched[0][:3] == ["vllm", "serve", str(swapper.models_root / "alpha")]
    _post(port, "/v1/chat/completions", {"model": "alpha", "stream": True, "messages": []})
    assert len(launched) == 1


def test_switching_models_stops_the_old_engine_and_uses_the_fallback(gpu):
    swapper, port, launched = gpu
    _post(port, "/v1/chat/completions", {"model": "alpha", "stream": True, "messages": []})
    old = swapper.proc
    status, body = _post(port, "/v1/chat/completions", {"model": "beta", "stream": False, "messages": []})
    assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "hi from beta"
    assert old is not None and old.poll() is not None
    assert swapper.active == "beta"
    assert [command[-1] for command in launched] == ["0.3", "--fail", "0.2"]


def test_load_progress_and_failure_are_reported(gpu):
    swapper, port, _launched = gpu
    status, body = _post(port, "/lc/load", {"model": "slow"})
    events = _events(body)
    assert status == 200 and events[0]["status"] == "loading" and events[-1] == {"status": "ready", "model": "slow"}
    assert any(event.get("seconds", 0) >= 0 for event in events[:-1])
    _status, failed = _post(port, "/lc/load", {"model": "broken"})
    last = _events(failed)[-1]
    assert last["status"] == "failed" and "unsupported flag" in last["detail"]
    assert _get(port, "/health")["status"] == "failed"


def test_streaming_keeps_the_connection_alive_while_loading(gpu):
    _swapper, port, _launched = gpu
    _status, body = _post(port, "/v1/chat/completions", {"model": "slow", "stream": True, "messages": []})
    assert ": loading the model" in body
    assert _events(body)[0]["choices"][0]["delta"]["content"] == "hi from slow"


def test_unknown_models_are_refused_plainly(gpu):
    _swapper, port, launched = gpu
    _status, body = _post(port, "/v1/chat/completions", {"model": "nope", "stream": True, "messages": []})
    error = _events(body)[0]["error"]
    assert "not in the model library" in error["message"] and "alpha" in error["message"]
    status, plain = _post(port, "/v1/chat/completions", {"model": "nope", "messages": []})
    assert status == 503 and "not in the model library" in plain
    assert launched == []


def test_new_downloads_appear_without_a_restart(gpu):
    swapper, port, _launched = gpu
    path = Path(swapper.models_root) / "models.json"
    library = json.loads(path.read_text())
    library["models"].append({"id": "gamma", "dir": "gamma", "args": []})
    path.write_text(json.dumps(library))
    assert "gamma" in [row["id"] for row in _get(port, "/v1/models")["data"]]
