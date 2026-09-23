"""One bounded continuation from ep-20260922-081807-faf7 through app-service."""
from __future__ import annotations

import json
import time
from pathlib import Path

from local_coder import BUILD_ID, __version__
from local_coder.app_service import AppRuntime
from local_coder.general.candidate_check import check_candidate, run_project_build
from local_coder.general.coordinator import project_id_for, review_payload
from local_coder.general.preview import check_preview_health, read_preview
from local_coder.machine import load_machine_config
from local_coder.server import server_status

PROJECT = Path.home() / "Local Coder Projects" / "task-board-3"
CONTINUE_FROM = "ep-20260922-081807-faf7"
EVIDENCE = Path.home() / ".local/share/local-coder/evidence/bonsai-taskboard-2026-09-22-cont"
REQUEST = (
    "Finish this task board so I can add tasks, edit their details, "
    "move them between the three columns, search them, and refresh the page "
    "without losing anything. Fix the existing errors and keep the interface "
    "clear and usable."
)
MAX_GENS = 6
WALL_S = 20 * 60


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def wait_runtime(runtime: AppRuntime, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        thread = runtime._thread
        if thread is None or not thread.is_alive():
            return "error" if runtime.last_error else "done"
        time.sleep(0.5)
    runtime.cancel()
    return "timeout"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    cfg = load_machine_config()
    status = server_status(cfg)
    preview = read_preview() or {}
    live = (
        Path.home()
        / ".local/share/local-coder/projects"
        / project_id_for(PROJECT)
        / "live"
        / "candidate"
    )
    pre_build = run_project_build(live) if live.is_dir() else {"ran": False}
    pre_check = check_candidate(live) if live.is_dir() else {}
    health = None
    if preview.get("url") and preview.get("dir"):
        health = check_preview_health(str(preview["url"]), Path(str(preview["dir"])))
    write_json(
        EVIDENCE / "preflight.json",
        {
            "helper": {"version": __version__, "build_id": BUILD_ID},
            "config": cfg.model_dump(),
            "server": status,
            "preview": preview,
            "preview_health": health,
            "pre_build": pre_build,
            "pre_check_detail": (pre_check or {}).get("detail") or (pre_build or {}).get("detail"),
            "continue_from": CONTINUE_FROM,
            "generation_limit": MAX_GENS,
            "wall_s": WALL_S,
        },
    )
    if not (status.get("healthy") and status.get("owned")):
        print(json.dumps({"stopped": "server", "status": status}, indent=2))
        return 2
    if int(cfg.n_ctx) != 8192 or int(cfg.max_tokens) != 3072:
        print(json.dumps({"stopped": "runtime_changed", "n_ctx": cfg.n_ctx, "max_tokens": cfg.max_tokens}, indent=2))
        return 2
    runtime = AppRuntime()
    events: list[dict] = []

    def on_event(event: dict) -> None:
        events.append({"t": time.time(), **event})
        print(f"{event.get('type')} {event.get('phrase') or event.get('data')}", flush=True)

    runtime.subscribe(on_event)
    t0 = time.time()
    started = runtime.start_request(
        str(PROJECT),
        REQUEST,
        engine="general",
        kind="create",
        generation_limit=MAX_GENS,
        continue_from=CONTINUE_FROM,
    )
    write_json(EVIDENCE / "started.json", started)
    print("started", started, flush=True)
    outcome = wait_runtime(runtime, WALL_S)
    elapsed = time.time() - t0
    episode = str(started.get("episode_id") or runtime.active_episode or "")
    review = review_payload(project_id_for(PROJECT), episode) if episode else {}
    write_json(EVIDENCE / "events.json", events[-200:])
    write_json(EVIDENCE / "review.json", review)
    write_json(
        EVIDENCE / "summary.json",
        {
            "outcome": outcome,
            "elapsed_s": round(elapsed, 2),
            "episode_id": episode,
            "error": runtime.last_error,
            "continue_from": CONTINUE_FROM,
            "generation_limit": MAX_GENS,
            "helper_build_id": BUILD_ID,
            "via": "app_service.AppRuntime.start_request",
            "verification": review.get("verification"),
            "model_commentary": review.get("model_commentary"),
        },
    )
    print(json.dumps({"outcome": outcome, "elapsed_s": round(elapsed, 2), "episode": episode}, indent=2))
    return 0 if outcome == "done" else 4


if __name__ == "__main__":
    raise SystemExit(main())
