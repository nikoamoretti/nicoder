"""Trusted coordinator: snapshots, local apply, review, Keep/Discard.

UI state stays here. The model writes file bodies; this module applies them
onto a disposable copy. The original tree changes only on Keep.
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
from local_coder.engines.opencode_server import client_from_record, read_record, stop_owned
from local_coder.general.budgets import RequestBudget, budget_for
from local_coder.general.candidate_check import (
    check_candidate,
    check_retry_message,
    empty_app_reason,
    run_project_build,
    write_verification,
)
from local_coder.general.direct_write import (
    CompletionResult,
    complete_files,
    fallback_explanation,
    local_complete,
    reply_prose,
)
from local_coder.general.inspect import classify_request, inspect_project
from local_coder.general.notebook import update_notebook
from local_coder.general.preview import check_preview_health, start_preview, stop_preview, write_preview_record
from local_coder.general.repair_context import (
    RepairState,
    build_repair_state,
    write_repair_state,
)
from local_coder.general.snapshot import (
    TreeIndex,
    apply_delta,
    compute_delta,
    copy_snapshot,
    index_tree,
    read_index,
    sync_source_tree,
    unified_diff,
    write_index,
)
from local_coder.general.source_check import parse_retry_message
from local_coder.general.starter import copy_starter
from local_coder.general.toolchain import ensure_ts_preview
from local_coder.machine import load_machine_config
from local_coder.persistence import new_episode_id
from local_coder.server import ServerError, server_status

EventListener = Callable[[dict[str, Any]], None]
MAX_CHECK_REPAIRS = 8


class _GenerationLedger:
    """Count live completions against the current request budget."""

    def __init__(self, budget: RequestBudget, generation_limit: int | None = None) -> None:
        self.budget = budget
        self.started = time.time()
        self.count = 0
        self.max_generations = (
            int(generation_limit) if generation_limit is not None else int(budget.max_generations)
        )

    def remaining_s(self) -> float:
        return max(0.0, float(self.budget.wall_seconds) - (time.time() - self.started))

    def can_generate(self) -> bool:
        return self.count < self.max_generations and self.remaining_s() > 8

    def snapshot(self) -> dict[str, Any]:
        return {
            "generations": self.count,
            "elapsed_s": round(time.time() - self.started, 2),
            "remaining_s": round(self.remaining_s(), 2),
            "max_generations": self.max_generations,
            "wall_seconds": self.budget.wall_seconds,
        }


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
        "preparing": "Making a working copy of the project.",
        "model_starting": "Loading the local model. This can take a minute.",
        "engine_starting": (
            str(data.get("detail") or "").strip()
            or ("Reusing the coding engine." if data.get("reused") else "Starting the coding engine.")
        ),
        "prompt_sent": (
            "The local model is writing code. Large models can sit here for several "
            "minutes while they read the project."
        ),
        "tool": str(data.get("detail") or f"Using {data.get('tool') or 'a tool'}."),
        "activity": str(data.get("detail") or "The model is working."),
        "working": (
            f"Still working — {data.get('elapsed_s', 0)} seconds so far. "
            f"{data.get('last') or 'Waiting for the next update.'}"
        ),
        "file_changed": f"Changed {data.get('path') or 'a file'}.",
        "check": "Checking the result.",
        "preview": "Opening a preview.",
        "budget": "Reached the time or step limit for this request.",
        "stopped": "Stopped.",
        "done": "Finished this attempt. Review the result.",
        "error": data.get("detail") or "Something went wrong.",
    }
    return str(mapping.get(event_type) or data.get("phrase") or event_type)


def live_dir(project_id: str) -> Path:
    """Sticky working copy reused across requests for the same project."""
    path = work_root(project_id) / "live"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_workspace(user_path: Path, request_id: str, *, allow_starter: bool) -> dict[str, Any]:
    """Snapshot the current user tree, including uncommitted work."""
    user_path = user_path.expanduser().resolve()
    pid = project_id_for(user_path)
    root = request_dir(pid, request_id)
    baseline = root / "baseline"
    candidate = root / "candidate"
    live_candidate = live_dir(pid) / "candidate"
    user_path.mkdir(parents=True, exist_ok=True)
    empty = not any(user_path.iterdir())
    starter_used = False
    if empty and allow_starter:
        copy_starter(user_path)
        starter_used = True
    b_index = copy_snapshot(user_path, baseline)
    # Real copies, not hardlinks: the engine writes the live tree, and a
    # shared inode would also mutate the frozen baseline.
    copy_snapshot(baseline, candidate)
    if not live_candidate.is_dir():
        copy_snapshot(baseline, live_candidate)
    # Candidate-only: Keep can persist SWC so Bonsai TypeScript compiles next time.
    ensure_ts_preview(candidate)
    ensure_ts_preview(live_candidate)
    write_index(root / "baseline.json", b_index)
    meta = {
        "request_id": request_id,
        "project_id": pid,
        "user_path": str(user_path),
        "baseline": str(baseline),
        "candidate": str(candidate),
        "engine_cwd": str(live_candidate),
        "starter_used": starter_used,
        "created_ts": time.time(),
        "engine": "local",
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
        "engine": "local",
        "pin": pin_record(),
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    if (historical_root / "meta.json").read_bytes() != hist_meta_bytes:
        raise GeneralError("historical meta.json changed during recovery copy", code="evidence")
    if (historical_root / "candidate" / "src" / "App.tsx").read_bytes() != sentinel:
        raise GeneralError("historical candidate changed during recovery copy", code="evidence")
    return meta


def _src_hashes(root: Path) -> dict[str, str]:
    """SHA-256 of candidate web sources. Used to protect continuation identity."""
    hashes: dict[str, str] = {}
    src = root / "src"
    if not src.is_dir():
        return hashes
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".tsx", ".ts", ".css", ".js", ".jsx"}:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def _align_continuation(meta: dict[str, Any], continue_from: str) -> None:
    """Reuse the historical candidate. Do not restart from the unchanged user folder."""
    hist = request_dir(str(meta["project_id"]), continue_from)
    if not hist.is_dir() or not (hist / "candidate").is_dir():
        raise GeneralError(
            f"cannot continue: {continue_from} has no candidate",
            code="evidence",
        )
    hist_cand = hist / "candidate"
    live = Path(meta.get("engine_cwd") or meta["candidate"])
    baseline = Path(meta["baseline"])
    hist_hashes = _src_hashes(hist_cand)
    live_hashes = _src_hashes(live) if live.is_dir() else {}
    user_hashes = _src_hashes(baseline)
    restored = False
    if hist_hashes and live_hashes == user_hashes and hist_hashes != user_hashes:
        copy_snapshot(hist_cand, live)
        restored = True
    elif hist_hashes and not live_hashes:
        copy_snapshot(hist_cand, live)
        restored = True
    _publish_candidate(live, Path(meta["candidate"]))
    after_hashes = _src_hashes(live)
    record: dict[str, Any] = {
        "continued_from": continue_from,
        "historical_hashes": hist_hashes,
        "live_hashes_before": live_hashes,
        "restored_live_from_historical": restored,
        "live_hashes_after": after_hashes,
        "candidate_hashes": _src_hashes(Path(meta["candidate"])),
    }
    request_root = Path(meta["baseline"]).parent
    (request_root / "continuation.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8",
    )
    write_index(request_root / "recovery_start.json", index_tree(Path(meta["candidate"])))
    meta["continued_from"] = continue_from
    meta["continuation"] = {
        "continued_from": continue_from,
        "restored_live_from_historical": restored,
        "historical_app": hist_hashes.get("src/App.tsx"),
        "live_app": after_hashes.get("src/App.tsx"),
    }
    _save_meta(meta)


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
        result = _start(cfg, wait_s=180)
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
    fake_complete: Callable[[str], str | CompletionResult] | None = None,
    start_preview_after: bool = True,
    generation_limit: int | None = None,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Snapshot, ask the model for files, apply them, then review."""
    folder = Path(user_path).expanduser().resolve()
    inspection = inspect_project(folder)
    classified = classify_request(text, inspection)
    if classified["kind"] == "empty":
        raise GeneralError(classified["summary"], code="empty")
    if classified["kind"] == "explain":
        from local_coder.app_service import explain_request

        return {"kind": "explain", **explain_request(folder, text)}
    if classified["kind"] == "chat":
        from local_coder.app_service import chat_request

        return chat_request(folder, text)
    request_id = request_id or new_episode_id()
    ledger = _GenerationLedger(budget_for(kind), generation_limit=generation_limit)
    try:
        model = resolve_model_id(model_id)
    except ModelIdentityError as exc:
        raise GeneralError(exc.detail, code=exc.code) from exc
    _emit(listener, "preparing", {"request_id": request_id})
    meta = prepare_workspace(folder, request_id, allow_starter=True)
    _emit(listener, "activity", {"detail": "Working copy is ready."})
    candidate = Path(meta["candidate"])
    engine_cwd = Path(meta.get("engine_cwd") or candidate)
    if continue_from:
        _align_continuation(meta, continue_from)
        candidate = Path(meta["candidate"])
        engine_cwd = Path(meta.get("engine_cwd") or candidate)
        _emit(listener, "activity", {
            "detail": f"Continuing from {continue_from} without restarting the original folder.",
        })
    update_notebook(request_dir(meta["project_id"], request_id), goal=text.strip())
    cancel = cancel_event or threading.Event()
    if fake_engine is not None:
        fake_engine(engine_cwd, text, cancel)
        _publish_candidate(engine_cwd, candidate)
        if not cancel.is_set():
            _verify_and_repair(
                meta, candidate, engine_cwd, text, inspection, listener, cancel, ledger,
                complete=None, conversation="", start_preview_after=start_preview_after,
                allow_repair=False,
            )
    else:
        if fake_complete is None:
            _emit(listener, "model_starting", {})
            ensure_model_server()
            try:
                assert_identity(model)
            except ModelIdentityError as exc:
                raise GeneralError(exc.detail, code=exc.code) from exc
        meta["model_id"] = model
        meta["engine"] = "local"
        (Path(meta["baseline"]).parent / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        from local_coder.general.chat_log import conversation_excerpt

        conversation = conversation_excerpt(folder)
        generate = _bound_complete(fake_complete or local_complete, ledger, cancel)
        if continue_from:
            _publish_candidate(engine_cwd, candidate)
            if not cancel.is_set():
                _verify_and_repair(
                    meta, candidate, engine_cwd, text, inspection, listener, cancel, ledger,
                    complete=generate, conversation=conversation,
                    start_preview_after=start_preview_after, allow_repair=True,
                )
        else:
            written, raw, problems = _apply_direct(
                engine_cwd, text, inspection, meta, listener, complete=generate,
                conversation=conversation, cancel=cancel,
            )
            truncated = any(item.startswith("truncated") for item in problems)
            if not written and not cancel.is_set() and ledger.can_generate() and not truncated:
                reason = parse_retry_message(problems) if problems else None
                written, raw, problems = _apply_direct(
                    engine_cwd, text, inspection, meta, listener,
                    complete=generate, previous=raw, conversation=conversation,
                    retry_reason=reason, cancel=cancel,
                )
                truncated = any(item.startswith("truncated") for item in problems)
            if not written:
                if cancel.is_set():
                    meta["model_commentary"] = "Stopped before files were applied."
                    meta["stopped_incomplete"] = True
                elif truncated:
                    meta["model_commentary"] = (
                        "The model reply was cut off at the output limit and was not applied. "
                        "Prefer a smaller focused edit. The original project is unchanged."
                    )
                    meta["stopped_incomplete"] = True
                    meta["truncated"] = True
                elif problems:
                    meta["model_commentary"] = (
                        "The model wrote files the preview cannot compile, so they were not applied. "
                        + problems[0]
                    )
                else:
                    meta["model_commentary"] = (
                        "The model was asked twice for files and did not return any."
                    )
                _save_meta(meta)
            _publish_candidate(engine_cwd, candidate)
            if written and not cancel.is_set():
                _verify_and_repair(
                    meta, candidate, engine_cwd, text, inspection, listener, cancel, ledger,
                    complete=generate, conversation=conversation,
                    start_preview_after=start_preview_after, allow_repair=True,
                )
            elif start_preview_after:
                _maybe_attach_preview(meta, candidate, listener, start_preview_after=start_preview_after)
    meta["generation_ledger"] = ledger.snapshot()
    _save_meta(meta)
    review = review_payload(meta["project_id"], request_id)
    from local_coder.general.chat_log import append_turn, load_chat

    append_turn(folder, "user", text.strip())
    note = str(review.get("model_commentary") or review.get("summary") or "").strip()
    if note:
        append_turn(folder, "assistant", note)
    review["turns"] = load_chat(folder)["turns"]
    _emit(listener, "done", {"request_id": request_id})
    return review


def _bound_complete(
    generate: Callable[[str], str | CompletionResult],
    ledger: _GenerationLedger,
    cancel: threading.Event,
) -> Callable[[str], str | CompletionResult]:
    """Count each model call against the request budget."""

    def wrapped(prompt: str) -> str | CompletionResult:
        if cancel.is_set():
            raise RuntimeError("cancelled")
        if not ledger.can_generate():
            raise RuntimeError("generation budget exhausted")
        ledger.count += 1
        return generate(prompt)

    return wrapped


def _verify_and_repair(
    meta: dict[str, Any],
    candidate: Path,
    engine_cwd: Path,
    text: str,
    inspection: dict[str, Any],
    listener: EventListener | None,
    cancel: threading.Event,
    ledger: _GenerationLedger,
    *,
    complete: Callable[[str], str | CompletionResult] | None,
    conversation: str,
    start_preview_after: bool,
    allow_repair: bool,
) -> None:
    """Check the candidate and, if needed, ask the model to fix it in this request."""
    repairs = 0
    same = 0
    empty = 0
    last_fp = ""
    last_raw = ""
    previous_repair: RepairState | None = None
    while True:
        if cancel.is_set():
            meta["stopped_incomplete"] = True
            _save_meta(meta)
            return
        check_started = time.perf_counter()
        verification = _record_verification(meta, candidate, listener, start_preview_after)
        meta.setdefault("timings", []).append({
            "check_s": round(time.perf_counter() - check_started, 3),
        })
        if verification.get("ok"):
            meta.pop("repair_state", None)
            _save_meta(meta)
            return
        repair = build_repair_state(text, verification, candidate, previous=previous_repair)
        previous_repair = repair
        write_repair_state(Path(meta["baseline"]).parent, repair)
        meta["repair_state"] = {
            "check": repair.check,
            "fingerprint": repair.fingerprint(),
            "hits": [{"rel": hit.rel, "line": hit.line} for hit in repair.hits],
            "candidate_id": repair.candidate_id,
        }
        fp = repair.fingerprint()
        if last_fp and fp == last_fp:
            same += 1
        else:
            same = 0
            last_fp = fp
        if not allow_repair or complete is None:
            meta["stopped_incomplete"] = True
            _save_meta(meta)
            return
        if (
            same >= 2
            or repairs >= MAX_CHECK_REPAIRS
            or not ledger.can_generate()
            or ledger.remaining_s() < 20
        ):
            meta["stopped_incomplete"] = True
            verification["stopped_incomplete"] = True
            meta["verification"] = verification
            _save_meta(meta)
            write_verification(Path(meta["baseline"]).parent, verification)
            _emit(listener, "budget", {"detail": "Stopped after checking. The original project is unchanged."})
            return
        _emit(listener, "activity", {"detail": "Sending the check error back to the model."})
        reason = check_retry_message(verification)
        if last_raw:
            reason = (
                "Your last reply had no write or edit blocks. "
                "Use fenced write, edit, or read blocks only. "
                "Do not use tool_call XML.\n" + reason
            )
        written, last_raw, problems = _apply_direct(
            engine_cwd, text, inspection, meta, listener, complete=complete,
            conversation=conversation, previous=last_raw or None, retry_reason=reason,
            repair=repair, cancel=cancel,
        )
        _publish_candidate(engine_cwd, candidate)
        if cancel.is_set() or (not written and problems == ["cancelled"]):
            meta["stopped_incomplete"] = True
            _save_meta(meta)
            return
        if any(item.startswith("truncated") for item in problems):
            meta["stopped_incomplete"] = True
            meta["truncated"] = True
            _save_meta(meta)
            return
        if not written:
            empty += 1
            if empty >= 2 or not ledger.can_generate():
                meta["stopped_incomplete"] = True
                _save_meta(meta)
                return
            continue
        empty = 0
        repairs += 1


def _record_verification(
    meta: dict[str, Any],
    candidate: Path,
    listener: EventListener | None,
    start_preview_after: bool,
) -> dict[str, Any]:
    """Run source/build/preview checks and persist evidence."""
    _emit(listener, "check", {})
    checks = check_candidate(candidate)
    files = sorted(compute_delta(_frozen_baseline(Path(meta["baseline"])), index_tree(candidate)).touched())
    preview_status = "not_run"
    page_mounted = False
    health: dict[str, Any] | None = None
    needs_preview = bool(start_preview_after and (candidate / "package.json").exists() and files)
    if not checks["source_ok"]:
        health = {"ok": False, "code": "source_check", "detail": checks.get("detail") or "source check failed"}
        preview_status = "failed"
        meta["preview_health"] = health
        meta["model_claim"] = meta.get("model_commentary")
        meta["model_commentary"] = _preview_failed_summary(str(health["detail"]), files or ["the candidate"])
    elif checks.get("empty_page"):
        health = {"ok": False, "code": "empty_page", "detail": checks["empty_page"]}
        preview_status = "failed"
        meta["preview_health"] = health
        meta["model_claim"] = meta.get("model_commentary")
        meta["model_commentary"] = _preview_failed_summary(str(checks["empty_page"]), files or ["the candidate"])
    elif needs_preview:
        _maybe_attach_preview(meta, candidate, listener, start_preview_after=True)
        stored = meta.get("preview_health") if isinstance(meta.get("preview_health"), dict) else None
        health = stored
        empty = empty_app_reason(candidate)
        if stored and stored.get("ok") and empty:
            health = {"ok": False, "code": "empty_page", "detail": empty}
            meta["preview_health"] = health
            meta["model_claim"] = meta.get("model_commentary")
            meta["model_commentary"] = _preview_failed_summary(empty, files or ["the candidate"])
            stored = health
        if stored and stored.get("ok"):
            preview_status = "ok"
            page_mounted = True
        elif stored:
            preview_status = "failed"
            health = stored
        if (candidate / "node_modules").is_dir() and not (checks.get("build") or {}).get("ran"):
            checks["build"] = run_project_build(candidate)
    if checks.get("build", {}).get("ran") and not checks.get("build", {}).get("ok"):
        if health is None or health.get("ok"):
            health = {"ok": False, "code": "build_failed", "detail": checks["build"].get("detail")}
            preview_status = "failed"
            meta["preview_health"] = health
            meta["model_claim"] = meta.get("model_commentary")
            meta["model_commentary"] = _preview_failed_summary(str(health["detail"]), files or ["the candidate"])
    build_status = "not_run"
    if checks.get("build", {}).get("ran"):
        build_status = "pass" if checks["build"].get("ok") else "fail"
    ok = bool(
        checks["source_ok"]
        and not checks.get("empty_page")
        and (not needs_preview or page_mounted)
        and (build_status != "fail")
    )
    verification = {
        "ok": ok,
        "files_written": bool(files),
        "build": build_status,
        "typecheck": "pass" if checks["source_ok"] else "fail",
        "tests": "not_run",
        "browser": "not_run",
        "preview": preview_status,
        "page_mounted": page_mounted,
        "behavior_verified": False,
        "stopped_incomplete": False,
        "detail": (health or {}).get("detail") or checks.get("detail") or "",
        "source_problems": checks.get("source_problems") or [],
        "build_detail": checks.get("build"),
        "preview_health": health,
    }
    meta["verification"] = verification
    _save_meta(meta)
    write_verification(Path(meta["baseline"]).parent, verification)
    return verification


def _publish_candidate(engine_cwd: Path, candidate: Path) -> None:
    """Copy live source onto the review candidate without wiping node_modules."""
    if engine_cwd.resolve() == candidate.resolve():
        return
    if candidate.exists() and (candidate / "node_modules").is_dir():
        sync_source_tree(engine_cwd, candidate)
        return
    copy_snapshot(engine_cwd, candidate)


def _save_meta(meta: dict[str, Any]) -> None:
    (Path(meta["baseline"]).parent / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )


def _write_last_reply(request_root: Path, raw: str) -> None:
    """Keep the latest model reply on disk, including truncated output."""
    (request_root / "last_reply.txt").write_text(raw or "", encoding="utf-8")


def _apply_direct(
    engine_cwd: Path,
    text: str,
    inspection: dict[str, Any],
    meta: dict[str, Any],
    listener: EventListener | None,
    *,
    complete: Callable[[str], str | CompletionResult] | None = None,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
    repair: RepairState | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[str], str, list[str]]:
    """Write model file bodies onto the working copy."""
    _emit(listener, "activity", {"detail": "Asking the model to write the files now."})
    apply_started = time.perf_counter()
    try:
        def on_event(kind: str, detail: str) -> None:
            if kind == "read":
                _emit(listener, "activity", {"detail": f"Reading {detail}."})
            elif kind == "edit":
                _emit(listener, "activity", {"detail": f"Edited {detail}."})
            elif kind == "completion":
                meta.setdefault("completions", []).append(detail)

        written, raw, problems = complete_files(
            engine_cwd, text, inspection, complete=complete, previous=previous,
            conversation=conversation, retry_reason=retry_reason, repair=repair,
            on_event=on_event, cancel=cancel,
        )
        _write_last_reply(Path(meta["baseline"]).parent, raw)
        meta.setdefault("timings", []).append({
            "apply_s": round(time.perf_counter() - apply_started, 3),
            "written": written,
        })
    except (RuntimeError, OSError) as exc:
        _emit(listener, "activity", {"detail": f"The model did not return usable files: {exc}"})
        return [], "", []
    if problems:
        if "cancelled" in problems:
            return [], raw, problems
        if any(item.startswith("truncated") for item in problems):
            _emit(listener, "activity", {
                "detail": "The model reply was cut off at the output limit and was not applied.",
            })
            return [], raw, problems
        _emit(listener, "activity", {
            "detail": "The model wrote files the preview cannot compile. They were not applied.",
        })
        return [], raw, problems
    if not written:
        _emit(listener, "activity", {"detail": "The model replied without file blocks."})
        return [], raw, []
    meta["model_commentary"] = reply_prose(raw) or fallback_explanation(written)
    _save_meta(meta)
    _emit(listener, "activity", {"detail": f"Wrote {written[0]}."})
    return written, raw, []


def _preview_failed_summary(detail: str, files: list[str]) -> str:
    excerpt = " ".join((detail or "").split())[:400]
    count = len(files)
    noun = "file" if count == 1 else "files"
    return (
        f"I wrote {count} {noun}, but the in-app preview does not run. "
        f"{excerpt} "
        "This is not a finished UI. The original project is unchanged. "
        "Keep only if you want these files anyway, or Discard."
    )


def _maybe_attach_preview(
    meta: dict[str, Any],
    candidate: Path,
    listener: EventListener | None,
    *,
    start_preview_after: bool,
) -> None:
    """Start preview after apply, then record an honest compile check."""
    if not start_preview_after or not (candidate / "package.json").exists():
        return
    if not _has_file_changes(Path(meta["baseline"]), candidate):
        return
    _emit(listener, "preview", {})
    preview = start_preview(candidate, str(meta["project_id"]))
    url = str(preview.get("url") or "").strip()
    if preview.get("ok") and url:
        health = check_preview_health(url, candidate)
    else:
        health = {
            "ok": False,
            "code": preview.get("code") or "preview_failed",
            "detail": preview.get("detail") or "The preview did not start.",
        }
    if preview.get("ok") and not health.get("ok"):
        preview = {
            **preview,
            "ok": False,
            "code": health.get("code") or "vite_overlay",
            "detail": health.get("detail") or "The preview does not run.",
        }
        if preview.get("pid"):
            write_preview_record(preview)
        _emit(listener, "activity", {
            "detail": "The preview does not run. Files were written, but this is not a finished UI.",
        })
    meta["preview_health"] = {
        "ok": bool(health.get("ok")),
        "code": health.get("code"),
        "detail": health.get("detail"),
    }
    if not health.get("ok"):
        files = sorted(compute_delta(_frozen_baseline(Path(meta["baseline"])), index_tree(candidate)).touched())
        meta["model_claim"] = meta.get("model_commentary")
        meta["model_commentary"] = _preview_failed_summary(str(health.get("detail") or ""), files or ["the candidate"])
    _save_meta(meta)


def _frozen_baseline(before: Path) -> TreeIndex:
    frozen = before.parent / "baseline.json"
    if frozen.is_file():
        return read_index(frozen)
    return index_tree(before)


def _has_file_changes(before: Path, after: Path) -> bool:
    """True when the working copy differs from the request baseline."""
    if not after.is_dir():
        return False
    if not before.is_dir() and not (before.parent / "baseline.json").is_file():
        return False
    return bool(compute_delta(_frozen_baseline(before), index_tree(after)).touched())


def review_payload(project_id: str, request_id: str) -> dict[str, Any]:
    meta = load_meta(project_id, request_id)
    baseline = Path(meta["baseline"])
    candidate = Path(meta["candidate"])
    frozen = baseline.parent / "baseline.json"
    before = read_index(frozen) if frozen.is_file() else index_tree(baseline)
    delta = compute_delta(before, index_tree(candidate))
    diff = unified_diff(baseline, candidate)
    files = sorted(delta.touched())
    commentary = str(meta.get("model_commentary") or "").strip() or None
    from local_coder.general.preview import read_preview

    preview: dict[str, Any] = {}
    current = read_preview() or {}
    if files and current.get("project_id") == project_id and current.get("dir") == str(candidate):
        preview = dict(current)
    health = meta.get("preview_health") if isinstance(meta.get("preview_health"), dict) else None
    if health and health.get("ok"):
        preview_status = "ok"
        if preview:
            preview["ok"] = True
    elif health:
        preview_status = "failed"
        preview = {
            "ok": False,
            "code": health.get("code") or "preview_failed",
            "detail": health.get("detail") or "The preview does not run.",
        }
        commentary = _preview_failed_summary(str(health.get("detail") or ""), files)
    elif not files:
        preview_status = "not_run"
    elif preview.get("ok"):
        preview_status = "ok"
    else:
        preview_status = "not_run"
    if files and preview_status == "failed":
        summary = commentary or _preview_failed_summary("", files)
    elif files:
        summary = commentary or (
            f"I changed {len(files)} file(s). Review the preview, then Keep or Discard."
        )
    elif commentary:
        summary = (
            "The model stopped after planning and did not write files. "
            "The original project was not modified. Send the request again, "
            "or ask for a smaller first step such as “Create src/App.tsx with a task list.”"
        )
    else:
        summary = "No file changes were recorded. The original project was not modified."
    stored: dict[str, Any] = {}
    raw_verification = meta.get("verification")
    if isinstance(raw_verification, dict):
        stored = raw_verification
    if stored.get("preview") in {"ok", "failed", "not_run"}:
        preview_status = str(stored.get("preview") or preview_status)
    return {
        "kind": "code",
        "engine": "local",
        "request_id": request_id,
        "episode_id": request_id,
        "preview": preview,
        "project_id": project_id,
        "repo_path": meta["user_path"],
        "summary": summary,
        "diff": diff,
        "facts": {
            "files": files,
            "delta": delta.as_dict(),
            "complete": False,
            "engine": "local",
            "model_id": meta.get("model_id"),
            "pin": meta.get("pin") or pin_record(),
        },
        "can_keep": bool(files),
        "can_discard": True,
        "verification": {
            "build": stored.get("build") or "not_run",
            "typecheck": stored.get("typecheck") or "not_run",
            "tests": stored.get("tests") or "not_run",
            "browser": stored.get("browser") or "not_run",
            "preview": preview_status,
            "page_mounted": bool(stored.get("page_mounted")),
            "behavior_verified": False,
            "stopped_incomplete": bool(meta.get("stopped_incomplete") or stored.get("stopped_incomplete")),
            "files_written": bool(files),
        },
        "model_commentary": commentary,
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
