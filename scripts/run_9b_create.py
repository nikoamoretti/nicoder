"""9B create trial: local model implements the task board; checks stay outside."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from local_coder.engines.opencode_client import OpenCodeError
from local_coder.engines.opencode_server import client_from_record, stop_owned
from local_coder.general.coordinator import run_general_request
from local_coder.general.starter import copy_starter
from local_coder.machine import load_machine_config

REQUEST_1 = (
    "Build a polished task-board web app. It needs three columns: To do, In progress, "
    "and Done. Let me add tasks, edit their title and description, move them between "
    "columns, delete them, and search them. Save my tasks so they remain after "
    "refreshing. Make the interface usable on desktop and a phone-sized screen. "
    "Run it so I can try it."
)

EVIDENCE = Path.home() / ".local/share/local-coder/evidence/9b-taskboard-2026-09-17"
USER_PROJECT = EVIDENCE / "app-user"
CREATE_BUDGET_S = 20 * 60


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def run_playwright(url: str, grep: str) -> dict:
    evals = Path("/Users/nico-yardlogix/projects/local-coder-16gb/evals/taskboard")
    t0 = time.time()
    proc = subprocess.run(
        ["npx", "playwright", "test", "checks.spec.ts", "--grep", grep],
        cwd=evals,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "TASKBOARD_URL": url},
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        "returncode": proc.returncode,
        "seconds": round(time.time() - t0, 2),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:],
        "url": url,
        "grep": grep,
    }


def continue_session(session_id: str, text: str, remaining_s: float) -> dict:
    cfg = load_machine_config()
    client = client_from_record()
    events: list[dict] = []
    t0 = time.time()
    client.prompt_async(session_id, text, provider_id="llama.cpp", model_id=cfg.model_alias)
    try:
        for event in client.events():
            etype = str(event.get("type") or "")
            events.append({"t": round(time.time() - t0, 2), "type": etype})
            print(f"feedback {time.time() - t0:6.1f}s {etype}", flush=True)
            if "idle" in etype or "session.error" in etype:
                break
            if time.time() - t0 > remaining_s:
                try:
                    client.abort(session_id)
                except OpenCodeError:
                    pass
                break
    except OpenCodeError as exc:
        events.append({"error": exc.detail})
    return {"seconds": round(time.time() - t0, 2), "events": events[-40:]}


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if USER_PROJECT.exists():
        raise SystemExit(f"refusing to reuse {USER_PROJECT}; delete it only if this is a fresh trial")
    USER_PROJECT.mkdir(parents=True)
    copy_starter(USER_PROJECT)
    print("stopping leftover OpenCode", stop_owned(), flush=True)
    events: list[dict] = []

    def listener(event: dict) -> None:
        events.append({"t": time.time(), **event})
        print(event.get("phrase") or event.get("type"), flush=True)

    started = time.time()
    result = run_general_request(
        USER_PROJECT,
        REQUEST_1,
        request_id=f"trial-create-9b-{int(started)}",
        kind="create",
        listener=listener,
        start_preview_after=True,
    )
    create_s = round(time.time() - started, 2)
    preview = (result.get("preview") or {}).get("url")
    payload = {
        "step": "create",
        "model": load_machine_config().model_alias,
        "seconds": create_s,
        "result": result,
        "events": events,
        "preview": preview,
    }
    write_json(EVIDENCE / "create.json", payload)
    print(json.dumps({
        "create_seconds": create_s,
        "files": result.get("facts", {}).get("files"),
        "preview": preview,
        "request_id": result.get("request_id"),
        "project_id": result.get("project_id"),
    }, indent=2), flush=True)
    if not preview:
        write_json(EVIDENCE / "create_checks.json", {"ok": False, "detail": "no preview"})
        return 2
    checks = run_playwright(preview, "request 1")
    write_json(EVIDENCE / "create_checks.json", checks)
    print("playwright", checks["returncode"], checks["seconds"], flush=True)
    print(checks["stdout"][-1500:], flush=True)
    remaining = CREATE_BUDGET_S - (time.time() - started)
    if checks["returncode"] == 0 or remaining < 90:
        return 0 if checks["returncode"] == 0 else 2
    session_id = None
    meta_path = None
    root = Path.home() / ".local/share/local-coder/projects" / str(result.get("project_id")) / "requests" / str(result.get("request_id"))
    meta_path = root / "meta.json"
    if meta_path.exists():
        session_id = json.loads(meta_path.read_text(encoding="utf-8")).get("session_id")
    if not session_id:
        return 2
    feedback = (
        "Independent browser checks failed on the running preview. Do not weaken "
        "the requirements. Observed failures:\n\n"
        f"{checks['stdout'][-2500:]}\n\n"
        "A created task must appear in the To do column. Editing, moving, searching, "
        "and refresh persistence must work. Fix the application in this project."
    )
    print(f"feeding failures back; remaining={remaining:.0f}s session={session_id}", flush=True)
    fb = continue_session(session_id, feedback, remaining)
    write_json(EVIDENCE / "create_feedback.json", fb)
    checks2 = run_playwright(preview, "request 1")
    write_json(EVIDENCE / "create_checks_after_feedback.json", checks2)
    print("playwright_after", checks2["returncode"], checks2["seconds"], flush=True)
    print(checks2["stdout"][-1500:], flush=True)
    return 0 if checks2["returncode"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
