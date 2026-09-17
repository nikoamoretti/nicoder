"""Run the required task-board demonstration. The local model writes the app."""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from local_coder.general.coordinator import keep_request, run_general_request
from local_coder.general.starter import copy_starter

REQUEST_1 = (
    "Build a polished task-board web app. It needs three columns: To do, In progress, "
    "and Done. Let me add tasks, edit their title and description, move them between "
    "columns, delete them, and search them. Save my tasks so they remain after "
    "refreshing. Make the interface usable on desktop and a phone-sized screen. "
    "Run it so I can try it."
)

REQUEST_2 = (
    "Add optional due dates and an Overdue filter. Overdue means a task has a due "
    "date before today and is not Done. Keep my existing tasks and preserve "
    "everything that already works."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--step", choices=("create", "follow_up"), default="create")
    parser.add_argument("--no-starter", action="store_true")
    args = parser.parse_args()
    project = Path(args.project)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    if args.step == "create" and not args.no_starter and not (project / "package.json").exists():
        if not any(project.iterdir()):
            copy_starter(project)
    events: list[dict] = []

    def listener(event: dict) -> None:
        events.append({"t": time.time(), **event})
        print(event.get("phrase") or event.get("type"), flush=True)

    text = REQUEST_1 if args.step == "create" else REQUEST_2
    t0 = time.time()
    result = run_general_request(
        project,
        text,
        request_id=f"trial-{args.step}-{int(t0)}",
        kind="create" if args.step == "create" else "follow_up",
        listener=listener,
        cancel_event=threading.Event(),
        start_preview_after=True,
    )
    payload = {
        "step": args.step,
        "seconds": round(time.time() - t0, 2),
        "result": result,
        "events": events,
    }
    (out / f"{args.step}.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "step": args.step,
        "seconds": payload["seconds"],
        "files": result.get("facts", {}).get("files"),
        "preview": (result.get("preview") or {}).get("url"),
        "request_id": result.get("request_id"),
        "project_id": result.get("project_id"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
