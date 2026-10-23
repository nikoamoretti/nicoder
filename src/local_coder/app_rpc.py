"""JSON-lines helper used by Local Coder.app.

One request object per stdin line:
  {"id": "1", "method": "inspect_project", "params": {"path": "..."}}

One response object per stdout line:
  {"id": "1", "ok": true, "result": {...}}
  {"id": "1", "ok": false, "error": {"code": "...", "detail": "..."}}

Unsolicited progress during a run or a question:
  {"id": null, "event": "progress", "payload": {...}}

`chat_request` and `orca_sign_in` run on their own threads, so `cancel` and
other calls are served meanwhile; their responses arrive when they are done.
The helper binds only stdin/stdout. Its one network listener is the loopback
callback `orca_sign_in` opens on 127.0.0.1 while the browser sign-in waits.
An OrcaRouter key passed to `orca_save_key` is never echoed or logged.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from . import BUILD_ID, __version__
from .app_service import (
    AppError,
    AppRuntime,
    build_worker_image,
    chat_request,
    classify_request,
    clear_project_chat,
    discard_changes,
    ensure_machine_config,
    ensure_server,
    explain_request,
    inspect_project,
    keep_changes,
    list_history,
    load_prefs,
    load_project_chat,
    open_docker_desktop,
    orca_models,
    orca_remove_key,
    orca_save_key,
    orca_sign_in,
    orca_status,
    readiness,
    remember_project,
    save_prefs,
    select_model,
    set_reasoning,
    snapshot,
    start_chat,
    stop_owned_server,
)
from .catalog import list_models
from .errorlog import NOT_FOUND, UNEXPECTED, record_error
from .general.coordinator import GeneralError
from .hostenv import apply_host_path
from .profiles import DEFAULT_PROFILE


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "result": result}


def _err(req_id: Any, code: str, detail: str) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "error": {"code": code, "detail": detail}}


class RpcServer:
    """Dispatch application methods for the Mac UI."""

    def __init__(self, runtime: AppRuntime | None = None, out: TextIO | None = None) -> None:
        self.runtime = runtime or AppRuntime()
        self.out = out or sys.stdout
        self.chat_cancel = threading.Event()
        self.sign_in_cancel = threading.Event()
        self._chat_thread: threading.Thread | None = None
        self._sign_in_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self.runtime.subscribe(self._on_event)

    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, default=str) + "\n"
        with self._write_lock:
            self.out.write(line)
            self.out.flush()

    def start_chat_thread(self, message: dict[str, Any]) -> threading.Thread | None:
        """Answer a question off the read loop so Stop and other calls still get through."""
        if self._chat_thread is not None and self._chat_thread.is_alive():
            self._write(_err(message.get("id"), "busy", "A question is already being answered."))
            return None
        self.chat_cancel.clear()
        thread = threading.Thread(
            target=lambda: self._write(self.dispatch(message)), name="local-coder-chat", daemon=True,
        )
        self._chat_thread = thread
        thread.start()
        return thread

    def start_sign_in_thread(self, message: dict[str, Any]) -> threading.Thread | None:
        """Wait for the browser sign-in off the read loop (it can take minutes)."""
        if self._sign_in_thread is not None and self._sign_in_thread.is_alive():
            self._write(_err(message.get("id"), "busy", "A sign-in is already waiting in the browser."))
            return None
        self.sign_in_cancel.clear()
        thread = threading.Thread(
            target=lambda: self._write(self.dispatch(message)), name="local-coder-sign-in", daemon=True,
        )
        self._sign_in_thread = thread
        thread.start()
        return thread

    def _on_event(self, event: dict[str, Any]) -> None:
        self._write({"id": None, "event": "progress", "payload": event})

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        """Run one method and return the response object."""
        req_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _err(req_id, "bad_request", "params must be an object")
        try:
            result = self._call(str(method), params)
        except (AppError, GeneralError) as exc:
            return _err(req_id, exc.code, exc.detail)
        except FileNotFoundError as exc:
            record_error(f"{method}: not found", exc)
            return _err(req_id, "not_found", NOT_FOUND)
        except Exception as exc:  # noqa: BLE001 — helper must never die on one call
            # One plain sentence for the app; the raw text and traceback go to stderr and logs/.
            record_error(f"{method}: unexpected error", exc)
            return _err(req_id, "internal", UNEXPECTED)
        return _ok(req_id, result)

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"ok": True, "service": "local-coder-app", "version": __version__, "build_id": BUILD_ID}
        if method == "inspect_project":
            report = inspect_project(params["path"])
            remember_project(report["path"]) if report.get("exists") else None
            return report
        if method == "classify_request":
            inspection = inspect_project(params["path"]) if params.get("path") else None
            return classify_request(params.get("text", ""), inspection)
        if method == "explain_request":
            return explain_request(params["path"], params["text"])
        if method == "chat_request":
            return chat_request(
                params.get("path") or None,
                str(params.get("text") or ""),
                listener=self._on_event,
                cancel=self.chat_cancel,
            )
        if method == "load_chat":
            return load_project_chat(params["path"])
        if method == "clear_chat":
            return clear_project_chat(params["path"])
        if method == "readiness":
            return readiness()
        if method == "list_models":
            return list_models()
        if method == "select_model":
            return select_model(str(params.get("id") or ""))
        if method == "set_reasoning":
            return set_reasoning(str(params.get("mode") or ""))
        if method == "ensure_machine":
            return ensure_machine_config()
        if method == "ensure_server":
            return ensure_server(wait_s=int(params.get("wait_s") or 300))
        if method == "prefs":
            return load_prefs()
        if method == "save_prefs":
            prefs = load_prefs()
            prefs.update({k: v for k, v in params.items() if k in {"profile", "recent_projects", "engine"}})
            save_prefs(prefs)
            return prefs
        if method == "history":
            return {"episodes": list_history(params["path"])}
        if method == "snapshot":
            return snapshot(params["episode_id"], repo=params.get("path"))
        if method == "run_request":
            return self.runtime.start_request(
                params["path"], params["text"],
                profile_name=params.get("profile") or load_prefs().get("profile") or DEFAULT_PROFILE,
                execution=params.get("execution"),
                engine=params.get("engine") or load_prefs().get("engine") or "general",
                kind=params.get("kind") or "create",
            )
        if method == "orca_status":
            return orca_status()
        if method == "orca_save_key":
            return orca_save_key(str(params.get("key") or ""))
        if method == "orca_remove_key":
            return orca_remove_key()
        if method == "orca_sign_in":
            return orca_sign_in(self._on_event, self.sign_in_cancel)
        if method == "orca_sign_in_cancel":
            self.sign_in_cancel.set()
            return {"ok": True}
        if method == "orca_models":
            return orca_models(refresh=bool(params.get("refresh")))
        if method == "cancel":
            self.chat_cancel.set()
            return self.runtime.cancel()
        if method == "keep":
            return keep_changes(params["episode_id"], params["path"])
        if method == "discard":
            return discard_changes(params["episode_id"], repo=params.get("path"))
        if method == "new_project":
            from local_coder.general.starter import create_project

            dest = create_project(params["parent"], params["name"])
            remember_project(dest)
            from local_coder.general.inspect import inspect_project as general_inspect

            return {"path": str(dest), "inspection": general_inspect(dest)}
        if method == "start_chat":
            parent = params.get("parent")
            return start_chat(str(params.get("text") or ""), parent=parent)
        if method == "preview_start":
            from local_coder.general.coordinator import project_id_for
            from local_coder.general.preview import start_preview

            folder = Path(params["path"])
            return start_preview(folder, params.get("project_id") or project_id_for(folder))
        if method == "preview_stop":
            from local_coder.general.preview import stop_preview

            return stop_preview()
        if method == "open_docker":
            return open_docker_desktop()
        if method == "build_worker":
            return build_worker_image(params["docker_dir"])
        if method == "stop_server":
            return stop_owned_server()
        if method == "shutdown":
            self.chat_cancel.set()
            self.sign_in_cancel.set()
            return self.runtime.shutdown()
        if method == "run_status":
            return {
                "running": self.runtime.is_running(),
                "episode_id": self.runtime.active_episode,
                "repo": self.runtime.active_repo,
                "last_error": self.runtime.last_error,
            }
        raise AppError(f"unknown method: {method}", code="bad_request")

    def serve(self, incoming: TextIO | None = None) -> int:
        """Read JSON lines until stdin closes."""
        apply_host_path()
        ensure_machine_config()
        handle = incoming or sys.stdin
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self._write(_err(None, "bad_request", "invalid JSON"))
                continue
            if not isinstance(message, dict):
                self._write(_err(None, "bad_request", "request must be an object"))
                continue
            if message.get("method") == "chat_request":
                self.start_chat_thread(message)
                continue
            if message.get("method") == "orca_sign_in":
                self.start_sign_in_thread(message)
                continue
            self._write(self.dispatch(message))
        self.chat_cancel.set()
        self.sign_in_cancel.set()
        return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for `local-coder app-rpc` and `python -m local_coder.app_rpc`."""
    del argv
    return RpcServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
