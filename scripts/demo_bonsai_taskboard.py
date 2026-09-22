"""Drive one task-board continuation through app-service, the helper the Mac window uses."""
from __future__ import annotations

import json
import time
from pathlib import Path

from local_coder.app_service import AppRuntime
from local_coder.machine import load_machine_config
from local_coder.server import server_status

PROJECT = Path.home() / "Local Coder Projects" / "task-board-3"
EVIDENCE = Path.home() / ".local/share/local-coder/evidence/bonsai-taskboard-2026-09-22"
REQUEST_1 = (
    "Finish this task board so I can add tasks, edit their details, "
    "move them between the three columns, search them, and refresh the page "
    "without losing anything. Fix the existing errors and keep the interface "
    "clear and usable."
)
REQUEST_2 = (
    "Add optional due dates and an Overdue filter. Preserve the tasks I've "
    "already saved and keep the existing features working."
)
MAX_GENS = 30
TOTAL_WALL_S = 45 * 60


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def wait_runtime(runtime: AppRuntime, started: dict, timeout_s: float, events: list) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        thread = runtime._thread
        if thread is None or not thread.is_alive():
            if runtime.last_error:
                return "error"
            return "done"
        time.sleep(0.5)
    runtime.cancel()
    return "timeout"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    cfg = load_machine_config()
    status = server_status(cfg)
    write_json(EVIDENCE / "runtime.json", {"config": cfg.model_dump(), "server": status})
    if not (status.get("healthy") and status.get("owned")):
        print(json.dumps({"stopped": "server", "status": status}, indent=2))
        return 2
    if int(cfg.n_ctx) != 8192:
        print(json.dumps({"stopped": "unexpected_n_ctx", "n_ctx": cfg.n_ctx}, indent=2))
        return 2
    runtime = AppRuntime()
    events: list[dict] = []

    def on_event(event: dict) -> None:
        events.append({"t": time.time(), **event})
        print(f"{event.get('type')} {event.get('phrase') or event.get('data')}", flush=True)

    runtime.subscribe(on_event)
    t0 = time.time()
    started = runtime.start_request(str(PROJECT), REQUEST_1, engine="general", kind="create")
    write_json(EVIDENCE / "req1-started.json", started)
    print("started", started, flush=True)
    outcome = wait_runtime(runtime, started, min(16 * 60, TOTAL_WALL_S), events)
    elapsed = time.time() - t0
    episode = str(started.get("episode_id") or runtime.active_episode or "")
    from local_coder.general.coordinator import project_id_for, review_payload

    review = review_payload(project_id_for(PROJECT), episode)
    write_json(EVIDENCE / "req1-events.json", events[-120:])
    write_json(EVIDENCE / "req1-review.json", review)
    write_json(
        EVIDENCE / "req1-summary.json",
        {
            "outcome": outcome,
            "elapsed_s": round(elapsed, 2),
            "episode_id": episode,
            "error": runtime.last_error,
            "path": str(PROJECT),
            "via": "app_service.AppRuntime.start_request",
            "verification": review.get("verification"),
            "generation_ledger": review.get("facts", {}),
        },
    )
    print(json.dumps({"outcome": outcome, "elapsed_s": round(elapsed, 2), "episode": episode}, indent=2))
    return 0 if outcome == "done" else 4


if __name__ == "__main__":
    raise SystemExit(main())
