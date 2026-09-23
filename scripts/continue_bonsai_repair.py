"""One bounded continuation from a recorded candidate through app-service (AppRuntime, not the window)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from local_coder import BUILD_ID, __version__
from local_coder.app_service import AppRuntime
from local_coder.general.coordinator import load_meta, project_id_for, request_dir, review_payload
from local_coder.general.preview import read_preview
from local_coder.general.snapshot import index_tree
from local_coder.machine import load_machine_config
from local_coder.server import server_status

PROJECT = Path.home() / "Local Coder Projects" / "task-board-3"
CONTINUE_FROM = os.environ.get("LC_CONTINUE_FROM", "ep-20260923-005611-9efd")
EVIDENCE = Path.home() / ".local/share/local-coder/evidence" / os.environ.get(
    "LC_EVIDENCE_NAME", "bonsai-taskboard-2026-09-23-read-cont",
)
REQUEST = (
    "Finish this task board so I can add tasks, edit their details, "
    "move them between the three columns, search them, and refresh the page "
    "without losing anything. Fix the existing errors and keep the interface "
    "clear and usable."
)
MAX_GENS = int(os.environ.get("LC_MAX_GENS", "5"))
WALL_S = int(os.environ.get("LC_WALL_S", str(18 * 60)))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def tree(root: Path) -> dict[str, str]:
    return {rel: entry.sha256 for rel, entry in index_tree(root).files.items()}


def memory_snapshot(pid: int | None) -> dict[str, object]:
    def run(argv: list[str]) -> str:
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"unavailable: {exc}"

    snap: dict[str, object] = {
        "t": time.time(),
        "swapusage": run(["sysctl", "vm.swapusage"]),
        "memory_pressure": run(["memory_pressure", "-Q"]).splitlines()[-1:],
        "vm_stat": run(["vm_stat"]).splitlines()[:12],
    }
    if pid:
        snap["llama_ps"] = run(["ps", "-o", "pid=,rss=,vsz=,pcpu=,etime=", "-p", str(pid)])
    return snap


def preview_probe(record: dict) -> dict[str, object]:
    url = str(record.get("url") or "")
    out: dict[str, object] = {"record": record}
    pid = int(record.get("pid") or 0)
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    out["pid_alive"] = alive
    if url.startswith("http://127.0.0.1:"):
        for path in ("/", "/src/App.tsx", "/src/TaskItem.tsx"):
            try:
                with httpx.Client(trust_env=False, timeout=5) as client:
                    response = client.get(url.rstrip("/") + path)
                out[path] = response.status_code
            except httpx.HTTPError as exc:
                out[path] = f"unreachable: {exc}"
    return out


def wait_runtime(runtime: AppRuntime, deadline: float) -> str:
    while time.monotonic() < deadline:
        thread = runtime._thread
        if thread is None or not thread.is_alive():
            return "error" if runtime.last_error else "done"
        time.sleep(0.5)
    runtime.cancel()
    return "deadline_cancelled"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    cfg = load_machine_config()
    status = server_status(cfg)
    pid = project_id_for(PROJECT)
    hist = request_dir(pid, CONTINUE_FROM)
    live = hist.parents[1] / "live" / "candidate"
    hist_tree = tree(hist / "candidate")
    preflight = {
        "via": "app_service.AppRuntime.start_request (API, not the Mac window)",
        "helper": {"version": __version__, "build_id": BUILD_ID, "python": sys.executable},
        "config": cfg.model_dump(),
        "server": status,
        "continue_from": CONTINUE_FROM,
        "historical_meta_stop": load_meta(pid, CONTINUE_FROM).get("stop_reason"),
        "historical_tree": hist_tree,
        "live_matches_historical": tree(live) == hist_tree,
        "user_tree": tree(PROJECT),
        "preview_before": preview_probe(read_preview() or {}),
        "memory_before": memory_snapshot(status.get("pid")),
        "generation_limit": MAX_GENS,
        "wall_s": WALL_S,
        "request": REQUEST,
    }
    write_json(EVIDENCE / "preflight.json", preflight)
    if not (status.get("healthy") and status.get("owned")):
        print(json.dumps({"stopped": "server", "status": status}, indent=2))
        return 2
    if int(cfg.n_ctx) != 8192 or int(cfg.max_tokens) != 3072:
        print(json.dumps({"stopped": "runtime_changed", "n_ctx": cfg.n_ctx, "max_tokens": cfg.max_tokens}))
        return 2
    runtime = AppRuntime()
    events: list[dict] = []

    def on_event(event: dict) -> None:
        events.append({"t": time.time(), **event})
        print(f"{time.strftime('%H:%M:%S')} {event.get('type')} {event.get('phrase') or event.get('data')}", flush=True)

    runtime.subscribe(on_event)
    t0 = time.time()
    started = runtime.start_request(
        str(PROJECT), REQUEST, engine="general", kind="create",
        generation_limit=MAX_GENS, continue_from=CONTINUE_FROM, wall_seconds=WALL_S,
    )
    print("started", started, flush=True)
    outcome = wait_runtime(runtime, time.monotonic() + WALL_S)
    ended_in_budget = outcome != "deadline_cancelled"
    if not ended_in_budget:
        thread = runtime._thread
        if thread is not None:
            thread.join(timeout=int(cfg.gen_timeout) + 60)
    elapsed = time.time() - t0
    episode = str(started.get("episode_id") or runtime.active_episode or "")
    root = request_dir(pid, episode)
    review = review_payload(pid, episode)
    meta = load_meta(pid, episode)
    for name in ("meta.json", "verification.json", "repair_state.json", "continuation.json", "last_reply.txt"):
        if (root / name).is_file():
            shutil.copy2(root / name, EVIDENCE / name)
    if (root / "turns").is_dir():
        shutil.copytree(root / "turns", EVIDENCE / "turns", dirs_exist_ok=True)
    write_json(EVIDENCE / "events.json", events)
    write_json(EVIDENCE / "review.json", review)
    summary = {
        "outcome": outcome,
        "ended_inside_wall_budget": ended_in_budget,
        "elapsed_s": round(elapsed, 2),
        "episode_id": episode,
        "error": runtime.last_error,
        "continue_from": CONTINUE_FROM,
        "generation_ledger": meta.get("generation_ledger"),
        "stop_reason": meta.get("stop_reason"),
        "replies": [
            {k: row.get(k) for k in ("turn", "kind", "files", "reads", "problems", "completion")}
            for row in meta.get("replies") or []
        ],
        "checks": meta.get("checks"),
        "timings": meta.get("timings"),
        "verification": review.get("verification"),
        "preview_after": preview_probe(read_preview() or {}),
        "user_tree_unchanged": tree(PROJECT) == preflight["user_tree"],
        "memory_after": memory_snapshot(status.get("pid")),
        "helper_build_id": BUILD_ID,
        "via": preflight["via"],
    }
    write_json(EVIDENCE / "summary.json", summary)
    print(json.dumps({k: summary[k] for k in ("outcome", "elapsed_s", "episode_id", "stop_reason")}, indent=2))
    return 0 if outcome == "done" else 4


if __name__ == "__main__":
    raise SystemExit(main())
