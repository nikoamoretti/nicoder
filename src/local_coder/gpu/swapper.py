"""Serve one model at a time on the GPU and swap on demand (runs inside the Cloud Run container).

Standard library only, so it runs on any engine image and is testable on a Mac.
The library (models.json in the bucket, mounted at /models) lists the models.
A request names one; if another model is loaded, the swapper waits for its
requests to finish, stops its engine, and starts the requested one (vLLM, or
llama.cpp for GGUF files). While a model loads, streaming requests receive SSE
comment lines so clients do not time out, and POST /lc/load reports progress.

Run: python -m local_coder.gpu.swapper --models-root /models --port $PORT
"""
from __future__ import annotations

import argparse
import collections
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .manifest import MANIFEST_NAME, find, parse

ENGINE_HOST = "127.0.0.1"
Launcher = Callable[[list[str]], subprocess.Popen[bytes]]


class LoadError(RuntimeError):
    """The requested model is unknown or its engine did not become ready."""


def _launch(command: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )


class Swapper:
    """Owns the engine process. One model at a time; loads are serialized."""

    def __init__(
        self,
        models_root: Path,
        *,
        engine_port: int = 8001,
        load_timeout: float = 1500.0,
        drain_timeout: float = 120.0,
        launcher: Launcher | None = None,
    ) -> None:
        self.models_root = Path(models_root)
        self.engine_port = engine_port
        self.load_timeout = load_timeout
        self.drain_timeout = drain_timeout
        self.launcher = launcher or _launch
        self.load_lock = threading.Lock()
        self.busy = threading.Condition()
        self.inflight = 0
        self.proc: subprocess.Popen[bytes] | None = None
        self.active: str | None = None
        self.tail: collections.deque[str] = collections.deque(maxlen=60)
        self.state: dict[str, Any] = {"status": "idle", "model": None, "since": time.time(), "detail": ""}

    def library(self) -> dict[str, Any]:
        """The current models.json (re-read every time, so new downloads appear without a restart)."""
        try:
            return parse((self.models_root / MANIFEST_NAME).read_bytes())
        except OSError:
            return parse(None)

    def command(self, entry: dict[str, Any], args: list[str]) -> list[str]:
        """The engine command line for one model."""
        folder = self.models_root / str(entry["dir"])
        port = str(self.engine_port)
        if entry.get("engine", "vllm") == "llamacpp":
            gguf = folder / str(entry.get("file") or "")
            return ["llama-server", "-m", str(gguf), "--alias", str(entry["id"]), "--host", ENGINE_HOST,
                    "--port", port, "--jinja", *args]
        return ["vllm", "serve", str(folder), "--served-model-name", str(entry["id"]), "--host", ENGINE_HOST,
                "--port", port, *args]

    def begin(self) -> None:
        """A request starts using the loaded model."""
        with self.busy:
            self.inflight += 1

    def end(self) -> None:
        """A request finished with the loaded model."""
        with self.busy:
            self.inflight -= 1
            self.busy.notify_all()

    def _drain(self) -> None:
        deadline = time.monotonic() + self.drain_timeout
        with self.busy:
            while self.inflight > 0 and time.monotonic() < deadline:
                self.busy.wait(timeout=1.0)

    def alive(self) -> bool:
        """Whether the engine process is running."""
        return self.proc is not None and self.proc.poll() is None

    def healthy(self, timeout: float = 3.0) -> bool:
        """Whether the engine answers its health check."""
        try:
            conn = http.client.HTTPConnection(ENGINE_HOST, self.engine_port, timeout=timeout)
            conn.request("GET", "/health")
            ok = conn.getresponse().status == 200
            conn.close()
            return ok
        except OSError:
            return False

    def ensure(self, model_id: str, progress: Callable[[float], None] | None = None) -> None:
        """Make `model_id` the loaded model. Raises LoadError."""
        entry = find(self.library(), model_id)
        if entry is None:
            names = ", ".join(str(row["id"]) for row in self.library()["models"]) or "none yet"
            raise LoadError(f"{model_id!r} is not in the model library (available: {names}).")
        with self.load_lock:
            if self.active == model_id and self.alive() and self.healthy():
                return
            self._drain()
            self.stop()
            attempts = [list(entry.get("args") or [])]
            if entry.get("fallback_args") is not None:
                attempts.append(list(entry["fallback_args"]))
            detail = ""
            for args in attempts:
                self.state = {"status": "loading", "model": model_id, "since": time.time(), "detail": ""}
                self.tail.clear()
                self.proc = self.launcher(self.command(entry, args))
                threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
                ready, detail = self._wait_ready(progress)
                if ready:
                    self.active = model_id
                    self.state = {"status": "ready", "model": model_id, "since": time.time(), "detail": ""}
                    return
                self.stop()
            self.state = {"status": "failed", "model": model_id, "since": time.time(), "detail": detail}
            raise LoadError(f"{model_id} did not start: {detail}")

    def _pump(self, proc: subprocess.Popen[bytes]) -> None:
        """Copy engine output to the container log and keep the last lines for errors."""
        if proc.stdout is None:
            return
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            self.tail.append(line)
            print(line, file=sys.stderr, flush=True)

    def _wait_ready(self, progress: Callable[[float], None] | None) -> tuple[bool, str]:
        started = time.monotonic()
        while time.monotonic() - started < self.load_timeout:
            if not self.alive():
                code = self.proc.returncode if self.proc is not None else None
                time.sleep(0.2)
                return False, f"engine exited (code {code}): " + " | ".join(list(self.tail)[-8:])
            if self.healthy(timeout=2.0):
                return True, ""
            if progress is not None:
                progress(time.monotonic() - started)
            time.sleep(1.0)
        return False, f"not ready after {int(self.load_timeout)} s: " + " | ".join(list(self.tail)[-8:])

    def stop(self) -> None:
        """Stop the engine (and anything it started)."""
        proc, self.proc, self.active = self.proc, None, None
        if proc is None or proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, 60.0), (signal.SIGKILL, 10.0)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    """OpenAI-compatible front door: /v1/chat/completions, /v1/completions, /v1/models, plus /lc/load."""

    protocol_version = "HTTP/1.1"
    swapper: Swapper
    keepalive_s = 5.0

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — BaseHTTPRequestHandler's name
        print(f"swapper: {self.address_string()} {format % args}", file=sys.stderr, flush=True)

    def do_GET(self) -> None:  # noqa: N802 — http.server naming
        if self.path == "/health":
            _json(self, 200, {"ok": True, **self.swapper.state})
        elif self.path == "/v1/models":
            rows = [
                {"id": row["id"], "object": "model", "owned_by": "local-coder", "name": row.get("name"),
                 "context": row.get("context")}
                for row in self.swapper.library()["models"]
            ]
            _json(self, 200, {"object": "list", "data": rows})
        else:
            _json(self, 404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802 — http.server naming
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            _json(self, 400, {"error": {"message": "body is not JSON"}})
            return
        if self.path == "/lc/load":
            self._load(str(payload.get("model") or ""))
        elif self.path in ("/v1/chat/completions", "/v1/completions"):
            self._proxy(raw, payload)
        else:
            _json(self, 404, {"error": {"message": f"no route {self.path}"}})

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: bytes) -> None:
        if data:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _load_in_background(self, model: str, on_wait: Callable[[float], None]) -> str:
        """Run ensure() on a thread, calling on_wait every keepalive_s; returns an error message or ''."""
        errors: list[str] = []
        started = time.monotonic()

        def work() -> None:
            try:
                self.swapper.ensure(model)
            except LoadError as exc:
                errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001 — reported to the client, never crashes the server
                errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(timeout=self.keepalive_s)
            if thread.is_alive():
                on_wait(time.monotonic() - started)
        return errors[0] if errors else ""

    def _load(self, model: str) -> None:
        self._start_stream()

        def report(seconds: float) -> None:
            event = {"status": "loading", "model": model, "seconds": round(seconds)}
            self._chunk(f"data: {json.dumps(event)}\n\n".encode())

        report(0.0)
        error = self._load_in_background(model, report)
        final = {"status": "failed", "model": model, "detail": error} if error else {"status": "ready", "model": model}
        self._chunk(f"data: {json.dumps(final)}\n\n".encode())
        self._end_stream()

    def _proxy(self, raw: bytes, payload: dict[str, Any]) -> None:
        model = str(payload.get("model") or "")
        stream = bool(payload.get("stream"))
        if stream:
            self._start_stream()
            error = self._load_in_background(model, lambda _seconds: self._chunk(b": loading the model\n\n"))
            if error:
                failure = {"error": {"message": error, "type": "model_load_failed"}}
                self._chunk(f"data: {json.dumps(failure)}\n\ndata: [DONE]\n\n".encode())
                self._end_stream()
                return
        else:
            try:
                self.swapper.ensure(model)
            except LoadError as exc:
                _json(self, 503, {"error": {"message": str(exc), "type": "model_load_failed"}})
                return
        self.swapper.begin()
        try:
            conn = http.client.HTTPConnection(ENGINE_HOST, self.swapper.engine_port, timeout=900)
            conn.request("POST", self.path, raw, {"Content-Type": "application/json"})
            response = conn.getresponse()
            if not stream:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type") or "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if response.status != 200:
                detail = response.read().decode("utf-8", errors="replace")[:2000]
                failure = {"error": {"message": f"engine HTTP {response.status}: {detail}", "type": "engine_error"}}
                self._chunk(f"data: {json.dumps(failure)}\n\ndata: [DONE]\n\n".encode())
            else:
                while True:
                    block = response.read1(65536)
                    if not block:
                        break
                    self._chunk(block)
            self._end_stream()
        except OSError as exc:
            if stream:
                failure = {"error": {"message": f"engine connection failed: {exc}", "type": "engine_error"}}
                self._chunk(f"data: {json.dumps(failure)}\n\ndata: [DONE]\n\n".encode())
                self._end_stream()
            else:
                _json(self, 502, {"error": {"message": f"engine connection failed: {exc}"}})
        finally:
            self.swapper.end()


def make_server(swapper: Swapper, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """An HTTP server bound to the swapper."""
    handler = type("BoundHandler", (Handler,), {"swapper": swapper})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    """Entry point inside the container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-root", default=os.environ.get("MODELS_ROOT", "/models"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--engine-port", type=int, default=8001)
    parser.add_argument("--preload", default=os.environ.get("PRELOAD_MODEL", ""))
    args = parser.parse_args(argv)
    swapper = Swapper(Path(args.models_root), engine_port=args.engine_port)
    server = make_server(swapper, args.port)

    def shutdown(_signum: int, _frame: Any) -> None:
        swapper.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    if args.preload:
        threading.Thread(target=lambda: _preload(swapper, args.preload), daemon=True).start()
    print(f"swapper: serving on :{args.port}, models in {args.models_root}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


def _preload(swapper: Swapper, model: str) -> None:
    try:
        swapper.ensure(model)
    except LoadError as exc:
        print(f"swapper: preload failed: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
