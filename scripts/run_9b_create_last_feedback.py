"""Last same-project feedback inside the remaining create budget."""
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
REMAINING_S = 5 * 60
PROMPT = """Independent browser check of the running preview at http://127.0.0.1:4554/:

The page title is Task Board but #root is empty. The console error is:
SyntaxError: The requested module '/src/utils/tasks.ts' does not provide an export named 'Task'

Playwright request-1 still fails all 6 tests (no To do heading, cannot add a task).

Fix the boot error first. Then a created task must appear in the To do column
immediately, editing/moving/searching must work, and tasks must survive refresh.
Do not dump large files or node_modules. Do not weaken the requirements.
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
    return {"returncode": proc.returncode, "seconds": round(time.time() - t0, 2), "stdout": proc.stdout[-3000:]}


def main() -> int:
    cfg = load_machine_config()
    record = read_record()
    if record and record.get("project") == str(CANDIDATE):
        client = client_from_record()
    else:
        server = OpenCodeServer(CANDIDATE, llama_endpoint=cfg.endpoint, model_id=cfg.model_alias)
        server.start(wait_s=40)
        client = server.client()
    session = client.create_session(title="create-last-feedback")
    session_id = str(session.get("id") or session.get("sessionID") or "")
    print("session", session_id, flush=True)
    t0 = time.time()
    client.prompt_async(session_id, PROMPT, provider_id="llama.cpp", model_id=cfg.model_alias)
    events: list[dict] = []
    try:
        for event in client.events():
            etype = str(event.get("type") or "")
            events.append({"t": round(time.time() - t0, 2), "type": etype})
            if etype not in {"server.heartbeat", "plugin.added", "catalog.updated", "message.part.delta"}:
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
    payload = {"session_id": session_id, "seconds": round(time.time() - t0, 2), "events": events[-50:]}
    (EVIDENCE / "create_last_feedback.json").write_text(json.dumps(payload, indent=2) + "\n")
    checks = run_playwright("http://127.0.0.1:4554/")
    (EVIDENCE / "create_checks_after_last_feedback.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps({"seconds": payload["seconds"], "playwright": checks["returncode"]}, indent=2), flush=True)
    print(checks["stdout"][-1500:], flush=True)
    return 0 if checks["returncode"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
