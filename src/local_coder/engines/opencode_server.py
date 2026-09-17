"""Owned OpenCode server lifecycle. Isolated HOME, loopback only."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from .opencode_client import OpenCodeClient, OpenCodeError
from .opencode_config import (
    DEFAULT_MODEL_ID,
    PROVIDER_ID,
    engine_home,
    ensure_pinned_binary,
    ensure_secret,
    isolated_env,
    opencode_config,
    overlay_content,
    write_engine_home,
)
from .opencode_pin import PINNED_VERSION, pin_record


class EngineServerError(Exception):
    """The OpenCode server could not be started or is not owned."""


def record_path() -> Path:
    from local_coder.machine import config_dir

    return config_dir() / "opencode-server.json"


def log_path() -> Path:
    from local_coder.machine import config_dir

    return config_dir() / "opencode-server.log"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def read_record() -> dict[str, Any] | None:
    path = record_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_record(data: dict[str, Any]) -> None:
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def client_from_record(record: dict[str, Any] | None = None) -> OpenCodeClient:
    row = record or read_record()
    if not row:
        raise EngineServerError("OpenCode server is not running.")
    return OpenCodeClient(str(row["url"]), "local-coder", ensure_secret())


class OpenCodeServer:
    """One owned `opencode serve --pure` process bound to loopback."""

    def __init__(
        self,
        project_dir: Path,
        *,
        llama_endpoint: str,
        model_id: str = DEFAULT_MODEL_ID,
        context: int = 32768,
        output: int = 2048,
        max_steps: int = 120,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.llama_endpoint = llama_endpoint.rstrip("/")
        self.model_id = model_id
        self.context = context
        self.output = output
        self.max_steps = max_steps
        self.proc: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.port = 0

    def start(self, *, wait_s: int = 30) -> dict[str, Any]:
        """Start or reuse an owned server whose cwd is the candidate project."""
        existing = read_record()
        if existing and _alive(int(existing.get("pid") or 0)):
            if existing.get("project") == str(self.project_dir):
                self.url = str(existing["url"])
                self.port = int(existing["port"])
                return {"started": False, "url": self.url, "pid": existing["pid"],
                        "reused": True, "pin": pin_record()}
            stop_owned()
        binary = ensure_pinned_binary()
        config = opencode_config(
            llama_base_url=self.llama_endpoint,
            model_id=self.model_id,
            context=self.context,
            output=self.output,
            max_steps=self.max_steps,
        )
        write_engine_home(config)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = isolated_env(extra={
            "OPENCODE_CONFIG_CONTENT": overlay_content(config),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_ENABLE_EXA": "0",
            "OPENCODE_ENABLE_PARALLEL": "0",
        })
        argv = [
            str(binary),
            "serve",
            "--pure",
            "--hostname", "127.0.0.1",
            "--port", str(self.port),
            "--log-level", "WARN",
        ]
        log_path().parent.mkdir(parents=True, exist_ok=True)
        with log_path().open("ab") as handle:
            self.proc = subprocess.Popen(
                argv,
                cwd=str(self.project_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + wait_s
        last_error = ""
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise EngineServerError(
                    f"OpenCode {PINNED_VERSION} exited early (code {self.proc.returncode}). "
                    f"See {log_path()}."
                )
            try:
                health = self.client().health()
            except Exception as exc:  # noqa: BLE001 — startup poll; failures are retried
                last_error = str(exc)
                time.sleep(0.25)
                continue
            write_record({
                "pid": self.proc.pid,
                "argv": argv,
                "url": self.url,
                "port": self.port,
                "project": str(self.project_dir),
                "model_id": self.model_id,
                "version": PINNED_VERSION,
                "home": str(engine_home()),
                "started_ts": time.time(),
            })
            return {
                "started": True,
                "url": self.url,
                "pid": self.proc.pid,
                "health": health,
                "pin": pin_record(),
            }
        self.stop()
        raise EngineServerError(
            f"OpenCode did not become healthy within {wait_s}s ({last_error}). See {log_path()}."
        )

    def client(self) -> OpenCodeClient:
        if not self.url:
            record = read_record()
            if not record:
                raise EngineServerError("OpenCode server is not running.")
            self.url = str(record["url"])
        return OpenCodeClient(self.url, "local-coder", ensure_secret())

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            _terminate(self.proc.pid)
        self.proc = None


def stop_owned() -> dict[str, Any]:
    """Stop only an OpenCode server this app started."""
    record = read_record()
    if record is None:
        return {"stopped": False, "detail": "no OpenCode ownership record"}
    pid = int(record.get("pid") or 0)
    if pid and _alive(pid):
        _terminate(pid)
    record_path().unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def _terminate(pid: int, grace_s: int = 8) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def httpx_error() -> tuple[type[BaseException], ...]:
    import httpx

    return (httpx.HTTPError, OSError)


def model_ref(model_id: str = DEFAULT_MODEL_ID) -> tuple[str, str]:
    return PROVIDER_ID, model_id
