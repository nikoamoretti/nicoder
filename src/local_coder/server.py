"""Local llama.cpp server lifecycle with explicit process ownership.

`server start` launches the configured binary with the configured model and
records pid + command line in a machine-local pid file. `server stop` only
signals a process this tool started and still recognizes. `server status`
reports ownership, health, and model identity without generating tokens.

Refusals are explicit: an occupied port owned by someone else, a missing
binary or model, or a pid file pointing at a different process never cause
a kill, a port takeover, or a silent adoption.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx

from .machine import MachineConfig, config_dir


class ServerError(Exception):
    """A server lifecycle operation was refused."""


def pid_path() -> Path:
    """Machine-local ownership record."""
    return config_dir() / "server.json"


def log_path() -> Path:
    """Server stdout/stderr capture path."""
    return config_dir() / "server.log"


def _read_pid_record() -> dict | None:
    path = pid_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_pid_record(pid: int, argv: list[str]) -> None:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "pid": pid, "argv": argv, "started_ts": time.time(),
    }, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _process_argv(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, ValueError):
        return ""
    return out.stdout.strip()


def _health(endpoint: str, timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            response = client.get(f"{endpoint}/health")
        return response.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _props(endpoint: str, timeout: float = 3.0) -> dict:
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            response = client.get(f"{endpoint}/props")
        if response.status_code == 200:
            return response.json()
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return {}


def server_status(cfg: MachineConfig) -> dict:
    """Report pid ownership, health, and model identity without generating."""
    record = _read_pid_record()
    status: dict = {
        "endpoint": cfg.endpoint,
        "owned": False,
        "running": False,
        "healthy": False,
        "pid": None,
        "model_alias": None,
        "note": "",
    }
    if record is not None:
        pid = int(record.get("pid", 0))
        if pid and _process_alive(pid):
            status.update({"owned": True, "running": True, "pid": pid})
        else:
            status["note"] = "stale pid file (process not running)"
    elif _health(cfg.endpoint):
        status.update({"running": True, "healthy": True,
                       "note": "endpoint is serving but was not started by local-coder; refusing ownership"})
        return status
    if status["running"]:
        status["healthy"] = _health(cfg.endpoint)
        if status["healthy"]:
            status["model_alias"] = _props(cfg.endpoint).get("model_alias")
    return status


def server_start(cfg: MachineConfig, wait_s: int = 90) -> dict:
    """Start the configured server once, recording ownership. No downloads."""
    record = _read_pid_record()
    if record is not None:
        pid = int(record.get("pid", 0))
        if pid and _process_alive(pid):
            raise ServerError(f"a server started by local-coder is already running (pid {pid})")
        pid_path().unlink(missing_ok=True)
    if _health(cfg.endpoint):
        raise ServerError(
            f"{cfg.endpoint} is already serving but was not started by local-coder; "
            "refusing to adopt or replace it (stop it yourself, or configure another endpoint)")
    if not cfg.model_file:
        raise ServerError("no model configured; run: local-coder setup --model-file PATH")
    model = Path(cfg.model_file).expanduser()
    if not model.exists():
        raise ServerError(f"model file missing: {model} (no automatic downloads)")
    binary = cfg.server_binary
    resolved = shutil.which(binary) or (binary if Path(binary).exists() else None)
    if resolved is None:
        raise ServerError(f"llama-server binary not found: {binary} (install llama.cpp; not auto-upgraded)")
    argv = [
        resolved,
        "-m", str(model),
        "--host", "127.0.0.1",
        "--port", _port(cfg.endpoint),
        "-c", str(max(cfg.n_ctx, 32768)),
        "--n-gpu-layers", "99",
        "--alias", cfg.model_alias,
        "--parallel", "1",
        "--jinja",
        "--reasoning", "off",
    ]
    log_path().parent.mkdir(parents=True, exist_ok=True)
    with log_path().open("ab") as handle:
        proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ServerError(f"server exited early (code {proc.returncode}); see {log_path()}")
        if _health(cfg.endpoint):
            _write_pid_record(proc.pid, argv)
            props = _props(cfg.endpoint)
            return {"pid": proc.pid, "endpoint": cfg.endpoint, "argv": argv,
                    "model_alias": props.get("model_alias"), "log": str(log_path())}
        time.sleep(2)
    _terminate(proc.pid)
    raise ServerError(f"server did not become healthy within {wait_s}s; see {log_path()}")


def server_stop() -> dict:
    """Stop only a process this tool started and still recognizes."""
    record = _read_pid_record()
    if record is None:
        raise ServerError("no local-coder server is running (no ownership record)")
    pid = int(record.get("pid", 0))
    if not pid or not _process_alive(pid):
        pid_path().unlink(missing_ok=True)
        return {"stopped": False, "detail": "stale pid file removed; process already gone"}
    expected = " ".join(str(a) for a in record.get("argv", []))
    actual = _process_argv(pid)
    if expected and expected not in actual and actual != expected:
        raise ServerError(
            f"pid {pid} does not match the recorded server command; refusing to signal it")
    _terminate(pid)
    pid_path().unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def _terminate(pid: int, grace_s: int = 15) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _port(endpoint: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    return str(parsed.port or 8080)
