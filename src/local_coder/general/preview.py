"""Owned Vite/dev-server preview on loopback. Preview is untrusted."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from local_coder.engines.opencode_config import data_root

PREVIEW_HOST = "127.0.0.1"


def preview_record_path() -> Path:
    from local_coder.machine import config_dir

    return config_dir() / "preview.json"


def project_port(project_id: str, *, base: int = 5173) -> int:
    """Stable loopback port derived from the project id."""
    digest = sum(ord(ch) for ch in project_id) + len(project_id) * 17
    return 4100 + (digest % 800)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def read_preview() -> dict[str, Any] | None:
    path = preview_record_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stop_preview() -> dict[str, Any]:
    """Stop only a preview this app started."""
    record = read_preview()
    if not record:
        return {"stopped": False, "detail": "no preview"}
    pid = int(record.get("pid") or 0)
    if pid and _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.2)
        if _alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    preview_record_path().unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def start_preview(project_dir: Path, project_id: str, *, wait_s: int = 45) -> dict[str, Any]:
    """Start `npm run dev` in the candidate, bound to a stable loopback port."""
    existing = read_preview()
    if existing and _alive(int(existing.get("pid") or 0)):
        if existing.get("project_id") == project_id and existing.get("dir") == str(project_dir):
            return {"started": False, **existing}
        stop_preview()
    port = project_port(project_id)
    if _port_open(PREVIEW_HOST, port):
        return {
            "ok": False,
            "code": "port_busy",
            "detail": f"Port {port} is already in use, so the preview was not started.",
            "url": f"http://{PREVIEW_HOST}:{port}/",
        }
    npm = _which("npm")
    if npm is None:
        return {"ok": False, "code": "npm_missing", "detail": "npm was not found on this Mac."}
    if not (project_dir / "package.json").exists():
        return {"ok": False, "code": "no_package", "detail": "This project has no package.json yet."}
    if not (project_dir / "node_modules").is_dir():
        install = subprocess.run(
            [npm, "install", "--no-fund", "--no-audit"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if install.returncode != 0:
            return {
                "ok": False,
                "code": "install_failed",
                "detail": (install.stderr or install.stdout)[-400:],
            }
    log = data_root() / "logs" / f"preview-{project_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["BROWSER"] = "none"
    env["HOST"] = PREVIEW_HOST
    env["PORT"] = str(port)
    argv = [npm, "run", "dev", "--", "--host", PREVIEW_HOST, "--port", str(port), "--strictPort"]
    with log.open("ab") as handle:
        proc = subprocess.Popen(
            argv,
            cwd=str(project_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = f"http://{PREVIEW_HOST}:{port}/"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"ok": False, "code": "preview_exited", "detail": f"Dev server exited. See {log}."}
        if _http_ok(url):
            record = {
                "ok": True,
                "started": True,
                "pid": proc.pid,
                "url": url,
                "port": port,
                "dir": str(project_dir),
                "project_id": project_id,
                "log": str(log),
            }
            preview_record_path().parent.mkdir(parents=True, exist_ok=True)
            preview_record_path().write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            return record
        time.sleep(0.4)
    stop_preview()
    return {"ok": False, "code": "preview_timeout", "detail": f"Preview did not start within {wait_s}s."}


def _http_ok(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            return False
        with httpx.Client(trust_env=False, timeout=1.0) as client:
            response = client.get(url)
        return response.status_code < 500
    except (httpx.HTTPError, OSError):
        return False


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)
