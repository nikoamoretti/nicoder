"""One same-project repair after the overflowed 9B create session died."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from local_coder.engines.opencode_client import OpenCodeError
from local_coder.engines.opencode_server import OpenCodeServer, client_from_record, read_record
from local_coder.machine import load_machine_config

EVIDENCE = Path.home() / ".local/share/local-coder/evidence/9b-taskboard-2026-09-17"
CANDIDATE = Path.home() / (
    ".local/share/local-coder/projects/app-user-cabb260102a3/"
    "requests/trial-create-9b-1789670226/candidate"
)
REMAINING_S = 12 * 60
PROMPT = """The previous attempt in this folder overflowed the 32768-token context
after bash/read tools and changed no application files. Independent Playwright
checks then failed all request-1 tests: there are no To do / In progress / Done
columns, created tasks do not appear, and nothing persists.

Implement the task-board now by writing small source files. Do not cat or dump
large trees, lockfiles, or node_modules. Do not run unbounded find/npm output.

Required behavior:
- Three columns: To do, In progress, Done
- Add, edit title/description, move, delete, and search tasks
- Persist tasks so they survive refresh
- Usable on desktop and a phone-sized screen
- A created task must appear in the correct column

Use the existing Vite + React + TypeScript starter. When a coherent change
exists, stop. Do not weaken the requirements.
"""


def run_playwright(url: str) -> dict:
    evals = Path("/Users/nico-yardlogix/projects/local-coder-16gb/evals/taskboard")
    t0 = time.time()
    proc = subprocess.run(
        ["npx", "playwright", "test", "checks.spec.ts", "--grep", "request 1"],
        cwd=evals,
        env={**os.environ, "TASKBOARD_URL": url},
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        "returncode": proc.returncode,
        "seconds": round(time.time() - t0, 2),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:],
    }


def main() -> int:
    cfg = load_machine_config()
    record = read_record()
    if record and record.get("project") == str(CANDIDATE):
        client = client_from_record()
    else:
        server = OpenCodeServer(CANDIDATE, llama_endpoint=cfg.endpoint, model_id=cfg.model_alias)
        server.start(wait_s=40)
        client = server.client()
    session = client.create_session(title="create-repair-after-overflow")
    session_id = str(session.get("id") or session.get("sessionID") or "")
    print("session", session_id, flush=True)
    t0 = time.time()
    client.prompt_async(session_id, PROMPT, provider_id="llama.cpp", model_id=cfg.model_alias)
    events: list[dict] = []
    try:
        for event in client.events():
            etype = str(event.get("type") or "")
            events.append({"t": round(time.time() - t0, 2), "type": etype})
            if etype not in {"server.heartbeat", "plugin.added", "catalog.updated"}:
                print(f"{time.time() - t0:6.1f}s {etype}", flush=True)
            if "idle" in etype or "session.error" in etype:
                break
            if time.time() - t0 > REMAINING_S:
                try:
                    client.abort(session_id)
                except OpenCodeError:
                    pass
                print("budget", flush=True)
                break
    except OpenCodeError as exc:
        events.append({"error": exc.detail})
    app = (CANDIDATE / "src" / "App.tsx").read_text(encoding="utf-8")
    payload = {
        "session_id": session_id,
        "seconds": round(time.time() - t0, 2),
        "app_bytes": len(app),
        "app_has_todo": "To do" in app or "Todo" in app or "to do" in app.lower(),
        "events": events[-80:],
    }
    (EVIDENCE / "create_repair.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("seconds", "app_bytes", "app_has_todo")}, indent=2), flush=True)
    preview = "http://127.0.0.1:4554/"
    checks = run_playwright(preview)
    (EVIDENCE / "create_checks_after_repair.json").write_text(
        json.dumps(checks, indent=2) + "\n", encoding="utf-8"
    )
    print("playwright", checks["returncode"], checks["seconds"], flush=True)
    print(checks["stdout"][-2000:], flush=True)
    return 0 if checks["returncode"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
