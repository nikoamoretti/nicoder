"""Trusted coordinator: snapshots, OpenCode sessions, review, Keep/Discard.

UI state stays here. OpenCode is not asked to accept into the original tree.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from local_coder.engines.model_identity import ModelIdentityError, assert_identity, resolve_model_id
from local_coder.engines.opencode_client import OpenCodeError
from local_coder.engines.opencode_config import data_root
from local_coder.engines.opencode_pin import pin_record
from local_coder.engines.opencode_server import (
    OpenCodeServer,
    client_from_record,
    read_record,
    stop_owned,
)
from local_coder.general.budgets import RequestBudget, budget_for
from local_coder.general.inspect import classify_request, inspect_project
from local_coder.general.notebook import update_notebook
from local_coder.general.preview import start_preview, stop_preview
from local_coder.general.snapshot import (
    apply_delta,
    compute_delta,
    copy_snapshot,
    index_tree,
    unified_diff,
    write_index,
)
from local_coder.general.starter import copy_starter
from local_coder.machine import load_machine_config
from local_coder.persistence import new_episode_id
from local_coder.server import ServerError, server_status

EventListener = Callable[[dict[str, Any]], None]


class GeneralError(Exception):
    def __init__(self, detail: str, code: str = "refused") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def project_id_for(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{path.resolve().name}-{digest}"


def work_root(project_id: str) -> Path:
    path = data_root() / "projects" / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def request_dir(project_id: str, request_id: str) -> Path:
    path = work_root(project_id) / "requests" / request_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _emit(listener: EventListener | None, event_type: str, data: dict[str, Any] | None = None) -> None:
    if listener is None:
        return
    listener({"type": event_type, "data": data or {}, "phrase": _phrase(event_type, data or {})})


def _phrase(event_type: str, data: dict[str, Any]) -> str:
    mapping = {
        "preparing": "Preparing a working copy…",
        "engine_starting": "Starting the local coding engine…",
        "model_starting": "Starting the local model…",
        "prompt_sent": "The local model is working…",
        "tool": f"Using {data.get('tool') or 'a tool'}…",
        "file_changed": f"Changed {data.get('path') or 'a file'}",
        "check": "Checking the result…",
        "preview": "Opening a preview…",
        "budget": "Reached the time or step limit for this request.",
        "stopped": "Stopped.",
        "done": "Finished this attempt. Review the result.",
        "error": data.get("detail") or "Something went wrong.",
    }
    return str(mapping.get(event_type) or data.get("phrase") or event_type)


def prepare_workspace(user_path: Path, request_id: str, *, allow_starter: bool) -> dict[str, Any]:
    """Snapshot the current user tree, including uncommitted work."""
    user_path = user_path.expanduser().resolve()
    pid = project_id_for(user_path)
    root = request_dir(pid, request_id)
    baseline = root / "baseline"
    candidate = root / "candidate"
    user_path.mkdir(parents=True, exist_ok=True)
    empty = not any(user_path.iterdir())
    starter_used = False
    if empty and allow_starter:
        copy_starter(user_path)
        starter_used = True
    b_index = copy_snapshot(user_path, baseline)
    copy_snapshot(user_path, candidate)
    write_index(root / "baseline.json", b_index)
    meta = {
        "request_id": request_id,
        "project_id": pid,
        "user_path": str(user_path),
        "baseline": str(baseline),
        "candidate": str(candidate),
        "starter_used": starter_used,
        "created_ts": time.time(),
        "engine": "opencode",
        "pin": pin_record(),
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def prepare_recovery_workspace(historical_root: Path | str, request_id: str) -> dict[str, Any]:
    """Copy a failed candidate into a new request without mutating the original."""
    historical_root = Path(historical_root).expanduser().resolve()
    hist_meta = json.loads((historical_root / "meta.json").read_text(encoding="utf-8"))
    sentinel = (historical_root / "candidate" / "src" / "App.tsx").read_bytes()
    hist_meta_bytes = (historical_root / "meta.json").read_bytes()
    user_path = Path(str(hist_meta["user_path"]))
    pid = str(hist_meta["project_id"])
    root = request_dir(pid, request_id)
    copy_snapshot(historical_root / "baseline", root / "baseline")
    copy_snapshot(historical_root / "candidate", root / "candidate")
    write_index(root / "baseline.json", index_tree(root / "baseline"))
    write_index(root / "recovery_start.json", index_tree(root / "candidate"))
    meta = {
        "request_id": request_id,
        "project_id": pid,
        "user_path": str(user_path),
        "baseline": str(root / "baseline"),
        "candidate": str(root / "candidate"),
        "starter_used": False,
        "recovered_from": str(historical_root),
        "created_ts": time.time(),
        "engine": "opencode",
        "pin": pin_record(),
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    if (historical_root / "meta.json").read_bytes() != hist_meta_bytes:
        raise GeneralError("historical meta.json changed during recovery copy", code="evidence")
    if (historical_root / "candidate" / "src" / "App.tsx").read_bytes() != sentinel:
        raise GeneralError("historical candidate changed during recovery copy", code="evidence")
    return meta


def load_meta(project_id: str, request_id: str) -> dict[str, Any]:
    path = request_dir(project_id, request_id) / "meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_model_server() -> dict[str, Any]:
    """Start the owned llama-server with tool-calling flags if needed."""
    from local_coder.server import server_start as _start

    cfg = load_machine_config()
    status = server_status(cfg)
    if status["healthy"] and status["owned"]:
        return {"ok": True, "started": False, "status": status}
    if status["healthy"] and not status["owned"]:
        raise GeneralError(
            f"A model server is already running at {cfg.endpoint} but Local Coder "
            "did not start it. Stop that server, or the app will not take it over.",
            code="foreign_server",
        )
    try:
        result = _start(cfg, wait_s=120)
    except ServerError as exc:
        raise GeneralError(str(exc), code="server") from exc
    return {"ok": True, "started": True, "status": server_status(cfg), "start": result}


def run_general_request(
    user_path: str | Path,
    text: str,
    *,
    request_id: str | None = None,
    kind: str = "create",
    listener: EventListener | None = None,
    cancel_event: threading.Event | None = None,
    model_id: str | None = None,
    fake_engine: Callable[[Path, str, threading.Event | None], None] | None = None,
    start_preview_after: bool = True,
) -> dict[str, Any]:
    """Run one bounded OpenCode request against a snapshot copy."""
    folder = Path(user_path).expanduser().resolve()
    inspection = inspect_project(folder)
    classified = classify_request(text, inspection)
    if classified["kind"] == "empty":
        raise GeneralError(classified["summary"], code="empty")
    if classified["kind"] == "explain":
        from local_coder.app_service import explain_request

        return {"kind": "explain", **explain_request(folder, text)}
    request_id = request_id or new_episode_id()
    budget = budget_for(kind)
    try:
        model = resolve_model_id(model_id)
    except ModelIdentityError as exc:
        raise GeneralError(exc.detail, code=exc.code) from exc
    _emit(listener, "preparing", {"request_id": request_id})
    meta = prepare_workspace(folder, request_id, allow_starter=True)
    candidate = Path(meta["candidate"])
    update_notebook(request_dir(meta["project_id"], request_id), goal=text.strip())
    cancel = cancel_event or threading.Event()
    started = time.monotonic()
    generations = 0
    if fake_engine is not None:
        fake_engine(candidate, text, cancel)
    else:
        _emit(listener, "model_starting", {})
        ensure_model_server()
        try:
            assert_identity(model)
        except ModelIdentityError as exc:
            raise GeneralError(exc.detail, code=exc.code) from exc
        _emit(listener, "engine_starting", {"pin": pin_record()})
        machine = load_machine_config()
        server = OpenCodeServer(
            candidate,
            llama_endpoint=machine.endpoint,
            model_id=model,
            context=machine.n_ctx,
            output=machine.max_tokens,
            max_steps=budget.max_generations,
        )
        server.start()
        client = server.client()
        session = client.create_session(title=text.strip()[:80])
        session_id = str(session.get("id") or session.get("sessionID") or session.get("session_id"))
        if not session_id:
            raise GeneralError("OpenCode did not return a session id.", code="engine")
        meta["session_id"] = session_id
        meta["model_id"] = model
        (Path(meta["baseline"]).parent / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        prompt = _user_prompt(text, inspection)
        client.prompt_async(session_id, prompt, provider_id="llama.cpp", model_id=model)
        _emit(listener, "prompt_sent", {"session_id": session_id})
        generations += 1
        _watch_session(
            client, session_id, budget, started, cancel, listener,
            generations_holder=lambda: generations,
            inc=lambda: _inc(generations),
        )
    review = review_payload(meta["project_id"], request_id)
    if start_preview_after and (candidate / "package.json").exists():
        _emit(listener, "preview", {})
        preview = start_preview(candidate, meta["project_id"])
        review["preview"] = preview
    _emit(listener, "done", {"request_id": request_id})
    return review


def _inc(value: int) -> int:
    return value + 1


def _user_prompt(text: str, inspection: dict[str, Any]) -> str:
    return (
        "You are working in this project folder. Implement the user's request using "
        "your normal file and command tools. Create and edit whatever files you need. "
        "Use React and TypeScript with the existing Vite tooling when this is a web app. "
        "Run the project's checks when you have a coherent change. Do not invent hidden "
        "cloud services. Do not require the user to run terminal commands.\n\n"
        f"Project: {inspection.get('name')}\n"
        f"Path: {inspection.get('path')}\n"
        f"Languages: {', '.join(inspection.get('languages') or []) or 'new or mixed'}\n\n"
        f"User request:\n{text.strip()}\n"
    )


def _watch_session(
    client: Any,
    session_id: str,
    budget: RequestBudget,
    started: float,
    cancel: threading.Event,
    listener: EventListener | None,
    *,
    generations_holder: Callable[[], int],
    inc: Callable[[], int],
) -> None:
    idle_deadline = time.monotonic() + budget.wall_seconds
    last_event = time.monotonic()
    try:
        for event in client.events():
            if cancel.is_set():
                try:
                    client.abort(session_id)
                except OpenCodeError:
                    pass
                _emit(listener, "stopped", {})
                return
            if time.monotonic() - started > budget.wall_seconds:
                client.abort(session_id)
                _emit(listener, "budget", {"reason": "wall_clock"})
                return
            last_event = time.monotonic()
            etype = str(event.get("type") or event.get("event") or "")
            payload = event.get("properties") or event.get("payload") or event
            if "permission" in etype and isinstance(payload, dict):
                permission_id = payload.get("id") or payload.get("permissionID")
                if permission_id:
                    try:
                        client.respond_permission(session_id, str(permission_id), "once")
                    except OpenCodeError:
                        pass
            if etype == "file.edited" and isinstance(payload, dict):
                _emit(listener, "file_changed", {"path": payload.get("file")})
            part = payload.get("part") if isinstance(payload, dict) else None
            tool_name = ""
            if isinstance(payload, dict):
                tool_name = str(payload.get("name") or payload.get("tool") or "")
            if isinstance(part, dict):
                tool_name = tool_name or str(part.get("tool") or part.get("name") or "")
            if tool_name:
                _emit(listener, "tool", {"tool": tool_name, "raw_type": etype})
            status = payload.get("status") if isinstance(payload, dict) else None
            if isinstance(status, dict) and status.get("type") == "idle":
                return
            if "session.idle" in etype or etype.endswith(".idle"):
                return
            if "session.error" in etype:
                _emit(listener, "error", {"detail": str(payload)})
                return
            if time.monotonic() - last_event > 90 and time.monotonic() > idle_deadline:
                return
    except OpenCodeError as exc:
        if cancel.is_set():
            _emit(listener, "stopped", {})
            return
        _emit(listener, "error", {"detail": exc.detail})


def review_payload(project_id: str, request_id: str) -> dict[str, Any]:
    meta = load_meta(project_id, request_id)
    baseline = Path(meta["baseline"])
    candidate = Path(meta["candidate"])
    delta = compute_delta(index_tree(baseline), index_tree(candidate))
    diff = unified_diff(baseline, candidate)
    files = sorted(delta.touched())
    from local_coder.general.preview import read_preview

    preview = read_preview() or {}
    return {
        "kind": "code",
        "engine": "opencode",
        "request_id": request_id,
        "episode_id": request_id,
        "preview": preview,
        "project_id": project_id,
        "repo_path": meta["user_path"],
        "summary": (
            f"Proposed changes in {len(files)} file(s). Review the preview and diff "
            "before keeping them."
            if files else
            "No file changes were recorded. The original project was not modified."
        ),
        "diff": diff,
        "facts": {
            "files": files,
            "delta": delta.as_dict(),
            "complete": bool(files),
            "engine": "opencode",
            "model_id": meta.get("model_id"),
            "pin": meta.get("pin") or pin_record(),
        },
        "can_keep": bool(files),
        "can_discard": True,
        "verification": {
            "build": "not_run",
            "typecheck": "not_run",
            "tests": "not_run",
            "browser": "not_run",
            "preview": "unknown",
        },
        "model_commentary": None,
        "accept_requires_confirmation": True,
    }


def keep_request(project_id: str, request_id: str, user_path: str | Path) -> dict[str, Any]:
    """Apply B→C onto the current user tree. Not an OpenCode revert."""
    meta = load_meta(project_id, request_id)
    result = apply_delta(Path(user_path), Path(meta["baseline"]), Path(meta["candidate"]))
    if not result.get("ok"):
        raise GeneralError(str(result.get("detail") or "Keep failed."), code=str(result.get("code") or "accept"))
    stop_owned()
    return {"ok": True, **result, "request_id": request_id, "repo": str(Path(user_path).resolve())}


def discard_request(project_id: str, request_id: str) -> dict[str, Any]:
    """Drop the candidate. Evidence remains. Session is not reused."""
    stop_owned()
    stop_preview()
    meta = load_meta(project_id, request_id)
    meta["discarded"] = True
    (request_dir(project_id, request_id) / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return {
        "ok": True,
        "discarded": True,
        "request_id": request_id,
        "detail": "Discarded. The original project was not changed.",
    }


def cancel_active() -> dict[str, Any]:
    record = read_record()
    if record and record.get("session_id"):
        try:
            client_from_record(record).abort(str(record["session_id"]))
        except (OpenCodeError, Exception):
            pass
    stop_owned()
    return {"ok": True, "detail": "Stop requested."}


def list_requests(user_path: str | Path) -> list[dict[str, Any]]:
    pid = project_id_for(Path(user_path))
    root = work_root(pid) / "requests"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for entry in root.iterdir():
        meta_path = entry / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({
            "episode_id": meta.get("request_id") or entry.name,
            "request_id": meta.get("request_id") or entry.name,
            "outcome": "discarded" if meta.get("discarded") else "review",
            "request": (meta.get("title") or "")[:160],
            "updated_ts": meta.get("created_ts"),
            "created_ts": meta.get("created_ts"),
            "complete": False,
        })
    rows.sort(key=lambda row: row.get("created_ts") or 0, reverse=True)
    return rows
