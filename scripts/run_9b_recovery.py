"""Recorded 9B recovery of the existing TaskBoard. Does not mutate history."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from local_coder.engines.opencode_client import OpenCodeError
from local_coder.engines.opencode_server import OpenCodeServer, current_proxy, stop_owned
from local_coder.general.coordinator import (
    ensure_model_server,
    prepare_recovery_workspace,
)
from local_coder.general.preview import start_preview
from local_coder.general.snapshot import compute_delta, index_tree
from local_coder.machine import load_machine_config

HISTORICAL = (
    Path.home()
    / ".local/share/local-coder/projects/app-user-cabb260102a3"
    / "requests/trial-create-9b-1789670226"
)
EVIDENCE = Path.home() / ".local/share/local-coder/evidence/9b-taskboard-recovery-2026-09-17"
EVALS = Path("/Users/nico-yardlogix/projects/local-coder-16gb/evals/taskboard")
REPO = Path("/Users/nico-yardlogix/projects/local-coder-16gb")

REQUEST_1 = (
    "Build a polished task-board web app. It needs three columns: To do, In progress, "
    "and Done. Let me add tasks, edit their title and description, move them between "
    "columns, delete them, and search them. Save my tasks so they remain after "
    "refreshing. Make the interface usable on desktop and a phone-sized screen. "
    "Run it so I can try it."
)
REQUEST_2 = (
    "Add optional due dates to tasks and an Overdue filter. "
    "Keep all existing task-board behavior and preserve the tasks that are already saved."
)

MAX_GENS = 30
MAX_PROBES = 2
TOTAL_WALL_S = 45 * 60
CREATE_WALL_S = 20 * 60
FOLLOW_WALL_S = 10 * 60
MODEL_ID = "Qwen3.5-9B-Q4_K_M"

TRANSPORT_MARKERS = (
    "providermodelnotfound",
    "apiconnection",
    "econn",
    "unreadable",
    "stream_contract",
    "context overflow",
    "contextoverflow",
    "failed to parse",
    "not json",
    "status code 5",
    "status code 4",
    "llama.cpp",
    "upstream",
)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def historical_fingerprints() -> dict[str, str]:
    files = [
        "meta.json",
        "candidate/src/App.tsx",
        "candidate/src/utils/tasks.ts",
        "candidate/src/components/TaskBoard.tsx",
    ]
    return {rel: sha256(HISTORICAL / rel) for rel in files}


class Ledger:
    """Count live chat completions across proxy restarts."""

    def __init__(self) -> None:
        self.started = time.time()
        self.closed = 0
        self.snapshots: list[dict[str, Any]] = []

    def note_stop(self) -> None:
        proxy = current_proxy()
        if proxy is not None:
            self.closed += len(proxy.history)
            self.snapshots.extend(list(proxy.history))

    def live(self) -> list[dict[str, Any]]:
        proxy = current_proxy()
        return list(proxy.history) if proxy is not None else []

    def history(self) -> list[dict[str, Any]]:
        return self.snapshots + self.live()

    def generations(self) -> int:
        return self.closed + len(self.live())

    def elapsed(self) -> float:
        return time.time() - self.started

    def remaining_total(self) -> float:
        return max(0.0, TOTAL_WALL_S - self.elapsed())

    def snapshot(self) -> dict[str, Any]:
        proxy = current_proxy()
        return {
            "generations": self.generations(),
            "elapsed_s": round(self.elapsed(), 2),
            "remaining_total_s": round(self.remaining_total(), 2),
            "dispatched": None if proxy is None else proxy.dispatched,
            "rejected": None if proxy is None else proxy.rejected,
            "cancelled": None if proxy is None else proxy.cancelled,
            "last_accounting": None if proxy is None else proxy.last_accounting,
            "history": self.history(),
        }


def run_cmd(argv: list[str], cwd: Path, timeout: int = 180) -> dict[str, Any]:
    t0 = time.time()
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "seconds": round(time.time() - t0, 2),
        "stdout": (proc.stdout or "")[-6000:],
        "stderr": (proc.stderr or "")[-3000:],
    }


def run_playwright(url: str, grep: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["TASKBOARD_URL"] = url
    t0 = time.time()
    proc = subprocess.run(
        ["npx", "playwright", "test", "checks.spec.ts", "--grep", grep],
        cwd=EVALS,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    return {
        "kind": "independent_check",
        "grep": grep,
        "url": url,
        "returncode": proc.returncode,
        "seconds": round(time.time() - t0, 2),
        "stdout": (proc.stdout or "")[-5000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def page_report(url: str, mode: str = "report") -> dict[str, Any]:
    env = os.environ.copy()
    env["TASKBOARD_URL"] = url
    proc = subprocess.run(
        ["node", "page-report.mjs", mode, url],
        cwd=EVALS,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload: dict[str, Any] = {
        "kind": "independent_check",
        "mode": mode,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-1500:],
    }
    try:
        payload["report"] = json.loads(proc.stdout)
    except ValueError:
        payload["stdout"] = (proc.stdout or "")[-2000:]
    return payload


def prechecks(candidate: Path) -> dict[str, Any]:
    npm = ["npm", "run"]
    install = run_cmd(["npm", "install", "--no-fund", "--no-audit"], candidate, timeout=180)
    build = run_cmd([*npm, "build"], candidate, timeout=180) if install["returncode"] == 0 else {
        "skipped": True, "reason": "install_failed",
    }
    return {"install": install, "build": build}


def looks_like_transport(detail: str) -> bool:
    lower = detail.lower()
    return any(marker in lower for marker in TRANSPORT_MARKERS)


def summarize_messages(messages: Any) -> dict[str, Any]:
    blob = json.dumps(messages, default=str)
    tools: list[str] = []
    texts: list[str] = []
    errors: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("tool") or node.get("name") or node.get("toolName")
            if name and str(name) not in tools and node.get("type") in {None, "tool", "tool-call", "tool_call"}:
                tools.append(str(name))
            if node.get("type") in {"tool", "tool-invocation", "tool_call"} and node.get("tool"):
                if str(node["tool"]) not in tools:
                    tools.append(str(node["tool"]))
            text = node.get("text") or node.get("content")
            if isinstance(text, str) and text.strip() and node.get("type") in {None, "text"}:
                texts.append(text[:400])
            err = node.get("error")
            if err:
                errors.append(str(err)[:400])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(messages)
    return {
        "tool_names": tools[:40],
        "text_snippets": texts[:12],
        "errors": errors[:12],
        "has_ready": "READY" in blob,
        "has_tool": bool(tools) or '"type": "tool"' in blob or "tool_call" in blob,
        "chars": len(blob),
    }


def watch_session(
    client: Any,
    session_id: str,
    ledger: Ledger,
    remaining_s: float,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    t0 = time.time()
    try:
        for event in client.events():
            elapsed = time.time() - t0
            etype = str(event.get("type") or event.get("event") or "")
            payload = event.get("properties") or event.get("payload") or event
            events.append({"t": round(elapsed, 2), "type": etype, "gens": ledger.generations()})
            print(f"{elapsed:7.1f}s gens={ledger.generations()} {etype}", flush=True)
            if "permission" in etype and isinstance(payload, dict):
                permission_id = payload.get("id") or payload.get("permissionID")
                if permission_id:
                    try:
                        client.respond_permission(session_id, str(permission_id), "once")
                    except OpenCodeError as exc:
                        events.append({"permission_error": exc.detail})
            if ledger.generations() >= MAX_GENS:
                try:
                    client.abort(session_id)
                except OpenCodeError:
                    pass
                return {"stop": "generations", "seconds": round(elapsed, 2)}
            if elapsed > remaining_s or ledger.remaining_total() < 5:
                try:
                    client.abort(session_id)
                except OpenCodeError:
                    pass
                return {"stop": "wall", "seconds": round(elapsed, 2)}
            if "session.error" in etype:
                detail = json.dumps(payload, default=str)[:2000]
                return {
                    "stop": "transport" if looks_like_transport(detail) else "session_error",
                    "detail": detail,
                    "seconds": round(elapsed, 2),
                }
            if isinstance(payload, dict):
                status = payload.get("status")
                if isinstance(status, dict) and status.get("type") == "idle":
                    return {"stop": "idle", "seconds": round(elapsed, 2)}
            if "session.idle" in etype or etype.endswith(".idle"):
                return {"stop": "idle", "seconds": round(elapsed, 2)}
    except OpenCodeError as exc:
        return {"stop": "transport", "detail": exc.detail, "seconds": round(time.time() - t0, 2)}
    return {"stop": "stream_ended", "seconds": round(time.time() - t0, 2)}


def prompt_session(
    client: Any,
    title: str,
    text: str,
    ledger: Ledger,
    remaining_s: float,
    evidence_name: str,
) -> dict[str, Any]:
    cfg = load_machine_config()
    events: list[dict[str, Any]] = []
    session = client.create_session(title=title[:80])
    session_id = str(session.get("id") or session.get("sessionID") or "")
    if not session_id:
        return {"ok": False, "stop": "transport", "detail": "OpenCode did not return a session id"}
    try:
        client.prompt_async(session_id, text, provider_id="llama.cpp", model_id=cfg.model_alias)
    except OpenCodeError as exc:
        return {
            "ok": False,
            "stop": "transport",
            "detail": exc.detail,
            "session_id": session_id,
        }
    watched = watch_session(client, session_id, ledger, remaining_s, events)
    messages = None
    summary = None
    try:
        messages = client.messages(session_id)
        summary = summarize_messages(messages)
        write_json(EVIDENCE / f"{evidence_name}-messages.json", _sanitize(messages))
    except OpenCodeError as exc:
        summary = {"errors": [exc.detail]}
        if watched.get("stop") != "transport":
            watched = {"stop": "transport", "detail": exc.detail, "seconds": watched.get("seconds")}
    transport_in_messages = bool(
        summary and summary.get("errors") and looks_like_transport(str(summary["errors"]))
    )
    result = {
        "ok": watched.get("stop") == "idle" and not transport_in_messages,
        "session_id": session_id,
        "watch": watched,
        "summary": summary,
        "events": events[-80:],
        "ledger": ledger.snapshot(),
    }
    if watched.get("stop") == "transport":
        result["ok"] = False
        result["stop"] = "transport"
        result["detail"] = watched.get("detail")
    write_json(EVIDENCE / f"{evidence_name}.json", result)
    return result


def _sanitize(payload: Any, limit: int = 1500) -> Any:
    if isinstance(payload, dict):
        return {key: _sanitize(value, limit) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_sanitize(item, limit) for item in payload[:80]]
    if isinstance(payload, str) and len(payload) > limit:
        return payload[:limit] + f"...<truncated {len(payload) - limit} chars>"
    return payload


def recovery_prompt(pre: dict[str, Any], report: dict[str, Any]) -> str:
    errors = []
    raw_report = report.get("report") or {}
    for item in raw_report.get("errors") or []:
        errors.append(str(item.get("text") or item)[:500])
    build = pre.get("build") or {}
    return (
        "This is a continuation of an unfinished local application. "
        "Do not start from a blank starter. The current project files are already "
        "in this workspace. Use your normal read/edit/bash tools. Read files in "
        "bounded slices. If a tool result is clipped, run "
        "`local-coder-observe <observation_id> <offset>` via bash to retrieve only "
        "the omitted slice. Do not dump or request the old transcript.\n\n"
        "Original application requirements:\n"
        f"{REQUEST_1}\n\n"
        "Short factual account of unfinished work:\n"
        "- A previous 9B session overflowed after large tool dumps "
        "(first prompt about 20526 tokens; a later request about 104101 tokens "
        "against a 32768 context).\n"
        "- Task-board files already exist (App.tsx mounts TaskBoard; "
        "src/components/TaskBoard.tsx, TaskCard, src/utils/tasks.ts).\n"
        "- Independent browser checks failed: the page did not show the three "
        "columns or title inputs.\n"
        "- Request 2 (due dates) must wait until this board actually works.\n\n"
        "Independent checks of the CURRENT files (not a guess):\n"
        f"build returncode={build.get('returncode')} stdout_tail:\n"
        f"{(build.get('stdout') or '')[-1800:]}\n"
        f"{(build.get('stderr') or '')[-800:]}\n\n"
        f"Browser page title={raw_report.get('title')!r} headings={raw_report.get('headings')}\n"
        f"Body excerpt:\n{(raw_report.get('body') or '')[:800]}\n\n"
        "Runtime/console errors:\n"
        f"{chr(10).join(errors) or '(none captured)'}\n\n"
        "Fix the application so a user can open a nonempty board, see To do / "
        "In progress / Done, add tasks that appear, edit details, move between "
        "columns, search/filter, and keep tasks after refresh. Run the project's "
        "own checks when you have a coherent change."
    )


def continue_session(
    client: Any,
    session_id: str,
    text: str,
    ledger: Ledger,
    remaining_s: float,
    evidence_name: str,
) -> dict[str, Any]:
    cfg = load_machine_config()
    events: list[dict[str, Any]] = []
    try:
        client.prompt_async(session_id, text, provider_id="llama.cpp", model_id=cfg.model_alias)
    except OpenCodeError as exc:
        result = {"ok": False, "stop": "transport", "detail": exc.detail, "session_id": session_id}
        write_json(EVIDENCE / f"{evidence_name}.json", result)
        return result
    watched = watch_session(client, session_id, ledger, remaining_s, events)
    try:
        messages = client.messages(session_id)
        summary = summarize_messages(messages)
        write_json(EVIDENCE / f"{evidence_name}-messages.json", _sanitize(messages))
    except OpenCodeError as exc:
        messages = None
        summary = {"errors": [exc.detail]}
    result = {
        "ok": watched.get("stop") == "idle",
        "session_id": session_id,
        "watch": watched,
        "summary": summary,
        "events": events[-80:],
        "ledger": ledger.snapshot(),
    }
    write_json(EVIDENCE / f"{evidence_name}.json", result)
    return result


def model_delta(start_index: Any, candidate: Path) -> dict[str, Any]:
    delta = compute_delta(start_index, index_tree(candidate))
    return {"kind": "model_generated", "delta": delta.as_dict(), "files": sorted(delta.touched())}


def is_hard_transport(result: dict[str, Any]) -> bool:
    """True only when the provider stream was unreadable. Abort after tokens is not that."""
    stop = result.get("stop") or (result.get("watch") or {}).get("stop")
    if stop != "transport":
        return False
    summary = result.get("summary") or {}
    if summary.get("has_ready") or summary.get("has_tool") or summary.get("text_snippets"):
        return False
    return True


def application_phase(
    *,
    client: Any,
    ledger: Ledger,
    record: dict[str, Any],
    candidate: Path,
    start_index: Any,
    url: str,
    preview: dict[str, Any],
    pre: dict[str, Any],
    report: dict[str, Any],
) -> int:
    print("recovery session", flush=True)
    create_started = time.time()
    recovery = prompt_session(
        client,
        "taskboard-recovery",
        recovery_prompt(pre, report),
        ledger,
        min(CREATE_WALL_S, ledger.remaining_total()),
        "recovery",
    )
    record["recovery"] = {
        "session_id": recovery.get("session_id"),
        "watch": recovery.get("watch"),
        "summary": recovery.get("summary"),
        "ledger": ledger.snapshot(),
    }
    if is_hard_transport(recovery):
        record["request1"] = {
            "ok": False,
            "blocked": "transport",
            "detail": recovery.get("detail") or recovery.get("watch", {}).get("detail"),
        }
        write_json(EVIDENCE / "summary.json", record)
        print(json.dumps({"stopped": "transport", "detail": record["request1"]["detail"]}, indent=2))
        return 2

    record["classification"]["model_generated"] = model_delta(start_index, candidate)
    after_report = page_report(url, "report") if url else {}
    write_json(EVIDENCE / "page-report-after-recovery.json", after_report)
    checks1 = run_playwright(url, "request 1") if url else {"ok": False, "detail": "no preview"}
    write_json(EVIDENCE / "create_checks.json", checks1)
    record["classification"]["independent_checks"].append("request-1-playwright")

    remaining_create = CREATE_WALL_S - (time.time() - create_started)
    if checks1.get("returncode") != 0 and remaining_create > 90 and ledger.remaining_total() > 90:
        if ledger.generations() < MAX_GENS:
            feedback = (
                "Independent checks of the running preview failed. Do not weaken "
                "the requirements. Observed failures:\n\n"
                f"{(checks1.get('stdout') or '')[-2500:]}\n\n"
                "Current runtime/console errors:\n"
                f"{json.dumps((after_report.get('report') or {}).get('errors'), default=str)[:1500]}\n\n"
                "A created task must appear in the To do column. Editing, moving, "
                "searching, and refresh persistence must work."
            )
            print("feeding factual failures back", flush=True)
            fb = continue_session(
                client,
                str(recovery["session_id"]),
                feedback,
                ledger,
                min(remaining_create, ledger.remaining_total()),
                "recovery-feedback",
            )
            record["recovery_feedback"] = {"watch": fb.get("watch"), "summary": fb.get("summary")}
            record["classification"]["model_generated"] = model_delta(start_index, candidate)
            after_report = page_report(url, "report") if url else {}
            write_json(EVIDENCE / "page-report-after-feedback.json", after_report)
            checks1 = run_playwright(url, "request 1") if url else checks1
            write_json(EVIDENCE / "create_checks_after_feedback.json", checks1)

    request1_ok = checks1.get("returncode") == 0
    record["request1"] = {
        "ok": request1_ok,
        "playwright": {"returncode": checks1.get("returncode"), "seconds": checks1.get("seconds")},
        "page": (after_report.get("report") or {}).get("errors"),
        "model_files": record["classification"]["model_generated"],
    }
    if not request1_ok:
        record["request2"] = {"ok": False, "blocked": "request_1_not_working"}
        record["ledger"] = ledger.snapshot()
        write_json(EVIDENCE / "summary.json", record)
        print(json.dumps({
            "request1": False,
            "request2": "not_run",
            "preview": url,
            "generations": ledger.generations(),
            "elapsed_s": round(ledger.elapsed(), 2),
            "candidate": str(candidate),
        }, indent=2), flush=True)
        return 4

    print("request 1 passed; seeding tasks before due dates", flush=True)
    seeded = page_report(url, "seed")
    write_json(EVIDENCE / "seeded-tasks.json", seeded)
    record["seeded"] = seeded.get("report") or seeded
    if ledger.generations() >= MAX_GENS or ledger.remaining_total() < 60:
        record["request2"] = {"ok": False, "blocked": "budget"}
        write_json(EVIDENCE / "summary.json", record)
        return 5

    print("request 2 follow-up", flush=True)
    follow = prompt_session(
        client,
        "taskboard-due-dates",
        REQUEST_2 + "\n\nExisting saved tasks must remain. Do not reset storage.",
        ledger,
        min(FOLLOW_WALL_S, ledger.remaining_total()),
        "follow-up",
    )
    record["classification"]["model_generated"] = model_delta(start_index, candidate)
    preserved = page_report(url, "report")
    write_json(EVIDENCE / "page-report-after-due-dates.json", preserved)
    checks2 = run_playwright(url, "request 2")
    write_json(EVIDENCE / "follow_checks.json", checks2)
    seed_ok = (
        (preserved.get("report") or {}).get("visibleSeededOpen", 0) > 0
        and (preserved.get("report") or {}).get("visibleKeepMe", 0) > 0
    )
    record["request2"] = {
        "ok": checks2.get("returncode") == 0 and seed_ok,
        "playwright": {"returncode": checks2.get("returncode"), "seconds": checks2.get("seconds")},
        "seeded_still_visible": seed_ok,
        "storage_after": (preserved.get("report") or {}).get("storage"),
        "follow": {"watch": follow.get("watch"), "summary": follow.get("summary")},
    }
    record["ledger"] = ledger.snapshot()
    record["preview"] = {"url": url, "pid": preview.get("pid"), "dir": str(candidate)}
    write_json(EVIDENCE / "summary.json", record)
    print(json.dumps({
        "request1": True,
        "request2": record["request2"]["ok"],
        "preview": url,
        "generations": ledger.generations(),
        "elapsed_s": round(ledger.elapsed(), 2),
        "candidate": str(candidate),
        "seeded_still_visible": seed_ok,
    }, indent=2), flush=True)
    return 0 if record["request2"]["ok"] else 6


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    before = historical_fingerprints()
    write_json(EVIDENCE / "historical-before.json", before)
    request_id = f"trial-recover-9b-{int(time.time())}"
    print("ensuring owned llama", flush=True)
    model = ensure_model_server()
    write_json(EVIDENCE / "model-server.json", model)
    print("stopping leftover OpenCode", stop_owned(), flush=True)
    meta = prepare_recovery_workspace(HISTORICAL, request_id)
    after = historical_fingerprints()
    write_json(EVIDENCE / "historical-after-copy.json", after)
    if after != before:
        raise SystemExit("historical candidate bytes changed during copy; refusing to continue")
    candidate = Path(meta["candidate"])
    start_index = index_tree(candidate)
    write_json(EVIDENCE / "meta.json", meta)
    print(f"recovery candidate {candidate}", flush=True)

    ledger = Ledger()
    record: dict[str, Any] = {
        "kind": "recorded_recovery_continuation",
        "not_from_scratch": True,
        "request_id": request_id,
        "historical": str(HISTORICAL),
        "candidate": str(candidate),
        "limits": {
            "max_generations": MAX_GENS,
            "total_wall_s": TOTAL_WALL_S,
            "create_wall_s": CREATE_WALL_S,
            "follow_wall_s": FOLLOW_WALL_S,
            "max_probes": MAX_PROBES,
        },
        "classification": {
            "model_generated": [],
            "tool_integration": [
                "src/local_coder/engines/stream_contract.py",
                "src/local_coder/engines/llama_proxy.py",
                "src/local_coder/engines/context_budget.py",
                "src/local_coder/engines/observations.py",
                "src/local_coder/engines/observe_cli.py",
            ],
            "independent_checks": [],
            "manual_intervention": [],
        },
    }

    print("independent prechecks", flush=True)
    pre = prechecks(candidate)
    write_json(EVIDENCE / "prechecks.json", pre)
    record["prechecks"] = {
        "install": pre["install"]["returncode"],
        "build": (pre.get("build") or {}).get("returncode"),
    }
    preview = start_preview(candidate, str(meta["project_id"]))
    write_json(EVIDENCE / "preview.json", preview)
    url = str(preview.get("url") or "")
    report = page_report(url, "report") if preview.get("ok") else {"ok": False, "detail": preview}
    write_json(EVIDENCE / "page-report-before.json", report)
    record["classification"]["independent_checks"].append("prechecks/build/page-report-before")

    cfg = load_machine_config()
    if cfg.n_ctx != 32768 or cfg.model_alias != MODEL_ID:
        raise SystemExit(f"refusing unexpected machine config {cfg.model_alias} n_ctx={cfg.n_ctx}")

    server = OpenCodeServer(
        candidate,
        llama_endpoint=cfg.endpoint,
        model_id=MODEL_ID,
        context=cfg.n_ctx,
        output=cfg.max_tokens,
        max_steps=MAX_GENS,
    )
    started = server.start(wait_s=30)
    write_json(EVIDENCE / "opencode-start.json", started)
    client = server.client()

    probe_ok = False
    probe_transport_fail = None
    for attempt in range(1, MAX_PROBES + 1):
        if ledger.generations() >= MAX_GENS or ledger.remaining_total() < 60:
            probe_transport_fail = "budget exhausted before live compatibility probe"
            break
        print(f"live compatibility probe {attempt}", flush=True)
        prompt = (
            "Compatibility probe only. Do not modify application files. "
            "Read src/App.tsx with your read tool, then reply with exactly the word READY."
            if attempt == 1
            else
            "Compatibility probe retry. Do not modify files. Reply with exactly READY."
        )
        probe = prompt_session(
            client,
            f"compat-probe-{attempt}",
            prompt,
            ledger,
            min(180.0, ledger.remaining_total()),
            f"probe-{attempt}",
        )
        write_json(EVIDENCE / f"probe-{attempt}-ledger.json", ledger.snapshot())
        summary = probe.get("summary") or {}
        if probe.get("stop") == "transport" or probe.get("watch", {}).get("stop") == "transport":
            if summary.get("has_ready") or summary.get("has_tool"):
                probe_ok = True
                record["probe"] = {
                    "attempt": attempt,
                    "ok": True,
                    "summary": summary,
                    "note": "provider response was readable; abort is not a transport failure",
                }
                break
            probe_transport_fail = probe.get("detail") or probe.get("watch", {}).get("detail")
            print(f"PROBE TRANSPORT FAILURE: {probe_transport_fail}", flush=True)
            continue
        if summary.get("has_ready") or summary.get("has_tool") or summary.get("text_snippets"):
            probe_ok = True
            record["probe"] = {"attempt": attempt, "ok": True, "summary": summary}
            break
        probe_transport_fail = (
            "unreadable or empty provider response: "
            + json.dumps(probe.get("watch"), default=str)[:500]
        )
    if not probe_ok:
        record["probe"] = {"ok": False, "detail": probe_transport_fail, "ledger": ledger.snapshot()}
        write_json(EVIDENCE / "summary.json", record)
        print(json.dumps({"stopped": "transport", "detail": probe_transport_fail}, indent=2), flush=True)
        return 2

    if ledger.generations() >= MAX_GENS or ledger.remaining_total() < 90:
        record["recovery"] = {"ok": False, "detail": "no budget left after probes"}
        write_json(EVIDENCE / "summary.json", record)
        return 3

    return application_phase(
        client=client,
        ledger=ledger,
        record=record,
        candidate=candidate,
        start_index=start_index,
        url=url,
        preview=preview,
        pre=pre,
        report=report,
    )


def continue_existing() -> int:
    """Resume the same attempt after a false transport stop. Does not reset the budget."""
    prior = json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))
    meta = json.loads((EVIDENCE / "meta.json").read_text(encoding="utf-8"))
    before = json.loads((EVIDENCE / "historical-before.json").read_text(encoding="utf-8"))
    after = historical_fingerprints()
    if after != before:
        raise SystemExit("historical candidate bytes changed; refusing to continue")
    candidate = Path(meta["candidate"])
    start_index = index_tree(candidate)
    pre = json.loads((EVIDENCE / "prechecks.json").read_text(encoding="utf-8"))
    report = json.loads((EVIDENCE / "page-report-before.json").read_text(encoding="utf-8"))
    preview = json.loads((EVIDENCE / "preview.json").read_text(encoding="utf-8"))
    url = str(preview.get("url") or "")
    prior_ledger = (prior.get("probe") or {}).get("ledger") or {}
    saved = {}
    saved_path = EVIDENCE / "attempt-ledger.json"
    if saved_path.exists():
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
    ledger = Ledger()
    ledger.started = float(meta.get("created_ts") or time.time())
    ledger.closed = int(saved.get("generations") or prior_ledger.get("generations") or 0)
    ledger.snapshots = list(saved.get("history") or prior_ledger.get("history") or [])
    record = prior
    record["probe"] = {
        "ok": True,
        "attempt": 2,
        "note": (
            "Both live probes returned a parsed READY/tool response. "
            "The runner had misclassified a 180s abort as transport failure."
        ),
        "probe1": "READY + native read tool",
        "probe2": "READY text",
        "prior_ledger": prior_ledger,
    }
    record["classification"]["manual_intervention"].append(
        "builder continued the same attempt after correcting the probe success test; "
        "no TaskBoard source was hand-edited"
    )
    print("stopping leftover OpenCode with a dead proxy", stop_owned(), flush=True)
    ensure_model_server()
    cfg = load_machine_config()
    server = OpenCodeServer(
        candidate,
        llama_endpoint=cfg.endpoint,
        model_id=MODEL_ID,
        context=cfg.n_ctx,
        output=cfg.max_tokens,
        max_steps=MAX_GENS,
    )
    started = server.start(wait_s=30)
    write_json(EVIDENCE / "opencode-restart.json", started)
    preview = start_preview(candidate, str(meta["project_id"]))
    write_json(EVIDENCE / "preview.json", preview)
    url = str(preview.get("url") or url)
    write_json(EVIDENCE / "summary.json", record)
    print(
        f"continuing recovery gens={ledger.generations()} "
        f"remaining={ledger.remaining_total():.0f}s candidate={candidate}",
        flush=True,
    )
    return application_phase(
        client=server.client(),
        ledger=ledger,
        record=record,
        candidate=candidate,
        start_index=start_index,
        url=url,
        preview=preview,
        pre=pre,
        report=report,
    )


if __name__ == "__main__":
    import sys

    if "--continue-existing" in sys.argv:
        raise SystemExit(continue_existing())
    raise SystemExit(main())
