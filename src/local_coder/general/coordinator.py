"""Trusted coordinator: snapshots, local apply, review, Keep/Discard.

UI state stays here. The model writes file bodies; this module applies them
onto a disposable copy. The original tree changes only on Keep.
"""
from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from local_coder.engines.model_identity import ModelIdentityError, assert_identity, resolve_model_id
from local_coder.engines.opencode_client import OpenCodeError
from local_coder.engines.opencode_config import data_root
from local_coder.engines.opencode_pin import pin_record
from local_coder.engines.opencode_server import client_from_record, read_record, stop_owned
from local_coder.general.budgets import RequestBudget, budget_for
from local_coder.general.candidate_check import (
    check_candidate,
    empty_app_reason,
    run_project_build,
    write_verification,
)
from local_coder.general.direct_write import (
    BudgetExhausted,
    CompletionResult,
    PromptTooLarge,
    ReplyOutcome,
    complete_files,
    fallback_explanation,
    local_complete,
    reply_prose,
)
from local_coder.general.inspect import classify_request, folder_problem, inspect_project
from local_coder.general.notebook import update_notebook
from local_coder.general.preview import check_preview_health, start_preview, stop_preview, write_preview_record
from local_coder.general.repair_context import (
    FileView,
    RepairState,
    build_repair_state,
    diagnostic_lines,
    error_keys,
    invalidate_views,
    write_repair_state,
)
from local_coder.general.request_state import DISCARDED, KEPT, WAITING, request_outcome, request_state
from local_coder.general.snapshot import (
    Delta,
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
from local_coder.hosted import provider_place
from local_coder.machine import is_hosted, load_machine_config
from local_coder.persistence import new_episode_id
from local_coder.server import ServerError, server_status

EventListener = Callable[[dict[str, Any]], None]
AgentChat = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]
MAX_CHECK_REPAIRS = 8
AGENT_REPAIR_ROUNDS = 2
AGENT_MAX_STEPS = 80
_VITE_CONFIGS = ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts")


class _GenerationLedger:
    """Count live completions against the current request budget."""

    def __init__(
        self,
        budget: RequestBudget,
        generation_limit: int | None = None,
        wall_seconds: int | None = None,
    ) -> None:
        self.budget = budget
        self.started = time.time()
        self.count = 0
        self.max_generations = (
            int(generation_limit) if generation_limit is not None else int(budget.max_generations)
        )
        self.wall_seconds = int(wall_seconds) if wall_seconds is not None else int(budget.wall_seconds)
        self.longest_generation_s = 0.0
        self.longest_check_s = 0.0

    def remaining_s(self) -> float:
        return max(0.0, float(self.wall_seconds) - (time.time() - self.started))

    def reserve_s(self) -> float:
        """Time one more model call plus its check is expected to need."""
        return max(8.0, self.longest_generation_s + self.longest_check_s)

    def can_generate(self) -> bool:
        return self.count < self.max_generations and self.remaining_s() > self.reserve_s()

    def stop_reason(self) -> str:
        if self.count >= self.max_generations:
            return "generation_limit"
        if self.remaining_s() <= self.reserve_s():
            return "wall_budget"
        return ""

    def note_generation(self, seconds: float) -> None:
        self.longest_generation_s = max(self.longest_generation_s, seconds)

    def note_check(self, seconds: float) -> None:
        self.longest_check_s = max(self.longest_check_s, seconds)

    def snapshot(self) -> dict[str, Any]:
        return {
            "generations": self.count,
            "elapsed_s": round(time.time() - self.started, 2),
            "remaining_s": round(self.remaining_s(), 2),
            "max_generations": self.max_generations,
            "wall_seconds": self.wall_seconds,
            "longest_generation_s": round(self.longest_generation_s, 2),
            "longest_check_s": round(self.longest_check_s, 2),
        }


class GeneralError(Exception):
    def __init__(self, detail: str, code: str = "refused") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")
_RUNNING: dict[str, int] = {}
_RUNNING_LOCK = threading.Lock()


@contextmanager
def _request_running(project_id: str) -> Iterator[None]:
    """Mark a project as having a request in flight, so Discard never rewrites its working copy under it."""
    with _RUNNING_LOCK:
        _RUNNING[project_id] = _RUNNING.get(project_id, 0) + 1
    try:
        yield
    finally:
        with _RUNNING_LOCK:
            remaining = _RUNNING.get(project_id, 1) - 1
            if remaining > 0:
                _RUNNING[project_id] = remaining
            else:
                _RUNNING.pop(project_id, None)


def _request_in_flight(project_id: str) -> bool:
    with _RUNNING_LOCK:
        return project_id in _RUNNING


def _tracks_running(function: Callable[_Params, _Result]) -> Callable[_Params, _Result]:
    """Wrap run_general_request: its first argument is the project folder."""

    @functools.wraps(function)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        folder = args[0] if args else kwargs["user_path"]
        with _request_running(project_id_for(Path(str(folder)).expanduser())):
            return function(*args, **kwargs)

    return wrapper


def project_id_for(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{path.resolve().name}-{digest}"


def project_dir(project_id: str) -> Path:
    """The project's folder under the data root. Creates nothing: reading must not leave folders behind."""
    return data_root() / "projects" / project_id


def work_root(project_id: str) -> Path:
    path = project_dir(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def request_dir(project_id: str, request_id: str) -> Path:
    path = work_root(project_id) / "requests" / request_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def request_meta_path(project_id: str, request_id: str) -> Path:
    """Where a request's meta.json is, or would be. Creates nothing."""
    return project_dir(project_id) / "requests" / request_id / "meta.json"


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
    """Snapshot the current user tree, including uncommitted work.

    The folder must already exist: a request never creates it. Only New Project and the welcome
    chat make folders.
    """
    user_path = user_path.expanduser().resolve()
    problem = folder_problem(user_path)
    if problem:
        raise GeneralError(problem["summary"], code=problem["verdict"])
    pid = project_id_for(user_path)
    root = request_dir(pid, request_id)
    baseline = root / "baseline"
    candidate = root / "candidate"
    live_candidate = live_dir(pid) / "candidate"
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


def _tree_hashes(root: Path) -> dict[str, str]:
    """Content identity of every snapshotted source file (node_modules and build output excluded)."""
    return {rel: entry.sha256 for rel, entry in index_tree(root).files.items()}


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
    candidate = Path(meta["candidate"])
    hist_tree = _tree_hashes(hist_cand)
    live_before = _tree_hashes(live) if live.is_dir() else {}
    restored = False
    if hist_tree != live_before:
        copy_snapshot(hist_cand, live)
        restored = True
    if _tree_hashes(live) != hist_tree:
        raise GeneralError(f"cannot continue: working copy does not match {continue_from}", code="evidence")
    _publish_candidate(live, candidate)
    candidate_tree = _tree_hashes(candidate)
    if candidate_tree != hist_tree:
        raise GeneralError(f"cannot continue: candidate does not match {continue_from}", code="evidence")
    hist_hashes = _src_hashes(hist_cand)
    after_hashes = _src_hashes(live)
    record: dict[str, Any] = {
        "continued_from": continue_from,
        "historical_hashes": hist_hashes,
        "historical_tree": hist_tree,
        "live_hashes_before": live_before,
        "restored_live_from_historical": restored,
        "live_hashes_after": after_hashes,
        "candidate_hashes": _src_hashes(candidate),
        "candidate_tree_matches": True,
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
    return json.loads(request_meta_path(project_id, request_id).read_text(encoding="utf-8"))


def ensure_model_server() -> dict[str, Any]:
    """Start the owned llama-server with tool-calling flags if needed. A hosted model needs none."""
    from local_coder.server import server_start as _start

    cfg = load_machine_config()
    if is_hosted(cfg):
        return {"ok": True, "started": False, "hosted": True}
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


@_tracks_running
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
    wall_seconds: int | None = None,
    agent_chat: AgentChat | None = None,
) -> dict[str, Any]:
    """Snapshot, ask the model for files, apply them, then review.

    With an agent-capable model (or `agent_chat` in tests), Local Coder's own
    agent edits the working copy with tools instead of the file-block path.
    """
    folder = Path(user_path).expanduser().resolve()
    problem = folder_problem(folder)
    if problem:
        raise GeneralError(problem["summary"], code=problem["verdict"])
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
    ledger = _GenerationLedger(budget_for(kind), generation_limit=generation_limit, wall_seconds=wall_seconds)
    try:
        model = resolve_model_id(model_id)
    except ModelIdentityError as exc:
        raise GeneralError(exc.detail, code=exc.code) from exc
    _emit(listener, "preparing", {"request_id": request_id})
    meta = prepare_workspace(folder, request_id, allow_starter=True)
    meta["title"] = _title_line(text)
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
    elif agent_chat is not None or (fake_complete is None and _agent_capable()):
        from local_coder.agent.runner import context_chars_for, hosted_tool_chat

        cfg = load_machine_config()
        if agent_chat is None:
            _emit(listener, "activity", {"detail": f"Using {model} on {provider_place(cfg)}."})
        meta["model_id"] = model
        meta["engine"] = "agent"
        meta["provider"] = cfg.provider
        _save_meta(meta)
        _run_agent_request(
            meta, folder, candidate, engine_cwd, text, inspection, listener, cancel, ledger,
            chat=agent_chat or hosted_tool_chat(cfg, cancel=cancel),
            context_chars=context_chars_for(cfg),
            fresh=not continue_from,
            start_preview_after=start_preview_after,
        )
    else:
        cfg = load_machine_config()
        if fake_complete is None:
            if is_hosted(cfg):
                _emit(listener, "activity", {"detail": f"Using {model} on {provider_place(cfg)}."})
            else:
                _emit(listener, "model_starting", {})
            ensure_model_server()
            try:
                assert_identity(model)
            except ModelIdentityError as exc:
                raise GeneralError(exc.detail, code=exc.code) from exc
        meta["model_id"] = model
        meta["engine"] = "local"
        meta["provider"] = cfg.provider
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
            written, raw, problems, _outcome = _apply_direct(
                engine_cwd, text, inspection, meta, listener, complete=generate,
                conversation=conversation, cancel=cancel,
            )
            truncated = any(item.startswith("truncated") for item in problems)
            if not written and not cancel.is_set() and ledger.can_generate() and not truncated:
                reason = parse_retry_message(problems) if problems else None
                written, raw, problems, _outcome = _apply_direct(
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
    stopped = cancel.is_set()
    if stopped:
        meta["stop_reason"] = "cancelled"
        meta["stopped_incomplete"] = True
        meta["model_commentary"] = stopped_text(_has_file_changes(Path(meta["baseline"]), candidate))
    meta["generation_ledger"] = ledger.snapshot()
    _save_meta(meta)
    review = review_payload(meta["project_id"], request_id)
    from local_coder.general.chat_log import append_turn, load_chat

    append_turn(folder, "user", text.strip())
    note = str(review.get("model_commentary") or review.get("summary") or "").strip()
    if note:
        append_turn(folder, "assistant", note)
    review["turns"] = load_chat(folder)["turns"]
    # A Stop is not a finished attempt: say "Stopped" and let the app show what was (not) written.
    _emit(listener, "stopped" if stopped else "done", {"request_id": request_id})
    return review


def _agent_capable() -> bool:
    from local_coder.agent.runner import agent_capable

    return agent_capable(load_machine_config())


def _is_vite_app(root: Path) -> bool:
    """Local Coder's own checks (source scan, build, preview) are built for Vite web apps."""
    return (
        (root / "package.json").is_file()
        and (root / "src").is_dir()
        and any((root / name).is_file() for name in _VITE_CONFIGS)
    )


def _counted_chat(chat: AgentChat, ledger: _GenerationLedger) -> AgentChat:
    """Count each agent model call against the request budget."""

    def counted(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        ledger.count += 1
        started = time.perf_counter()
        try:
            return chat(messages, tools)
        finally:
            ledger.note_generation(time.perf_counter() - started)

    return counted


def _agent_steps(ledger: _GenerationLedger) -> int:
    return max(1, min(AGENT_MAX_STEPS, ledger.max_generations - ledger.count))


def _check_feedback(meta: dict[str, Any]) -> str:
    """Local Coder's latest failed check, phrased for the agent's next turn."""
    checks = meta.get("checks") or []
    errors = [str(row) for row in (checks[-1].get("errors") or [])] if checks else []
    health = meta.get("preview_health") if isinstance(meta.get("preview_health"), dict) else {}
    detail = "\n".join(errors[:40]) or str((health or {}).get("detail") or "the check failed without details")
    return (
        "Local Coder checked the working copy (source check, build and preview) and it failed:\n"
        f"{detail[:6000]}\n"
        "Fix these problems in the working copy, run the build again, then reply."
    )


def _run_agent_request(
    meta: dict[str, Any],
    folder: Path,
    candidate: Path,
    engine_cwd: Path,
    text: str,
    inspection: dict[str, Any],
    listener: EventListener | None,
    cancel: threading.Event,
    ledger: _GenerationLedger,
    *,
    chat: AgentChat,
    context_chars: int,
    fresh: bool,
    start_preview_after: bool,
) -> None:
    """Local Coder's own agent edits the working copy; Local Coder checks what it wrote."""
    import shutil

    from local_coder.agent.runner import build_session
    from local_coder.general.chat_log import recent_messages

    request_root = Path(meta["baseline"]).parent
    baseline = Path(meta["baseline"])
    if fresh:
        # Start from the user's folder as it is now; node_modules in the live copy stay.
        sync_source_tree(baseline, engine_cwd)
    session = build_session(
        project=folder,
        workdir=engine_cwd,
        run_tmp=request_root / "agent-tmp",
        chat=_counted_chat(chat, ledger),
        context_chars=context_chars,
        history=recent_messages(folder, limit=10, max_chars=1500),
        cancel=cancel,
        on_step=lambda step: _emit(listener, "activity", step),
    )
    run = session.ask(text, max_steps=_agent_steps(ledger), deadline=time.monotonic() + ledger.remaining_s())
    web_app = _is_vite_app(engine_cwd)
    rounds = 0
    try:
        while True:
            _publish_candidate(engine_cwd, candidate)
            meta["model_commentary"] = run.answer
            if cancel.is_set() or run.status == "stopped":
                meta["model_commentary"] = stopped_text(_has_file_changes(baseline, candidate))
                _stop(meta, "cancelled")
                break
            if not _has_file_changes(baseline, candidate):
                break
            if not web_app:
                meta["verification"] = {
                    "local_checks": "not_run",
                    "detail": "Local Coder's own checks cover Vite web apps; the agent ran the project's commands.",
                    "agent_commands": session.toolbox.commands[-20:],
                }
                break
            for key in ("stopped_incomplete", "stop_reason", "preview_health"):
                meta.pop(key, None)
            _verify_and_repair(
                meta, candidate, engine_cwd, text, inspection, listener, cancel, ledger,
                complete=None, conversation="", start_preview_after=start_preview_after, allow_repair=False,
            )
            if meta.get("stop_reason") == "checks_passed" or cancel.is_set():
                break
            if rounds >= AGENT_REPAIR_ROUNDS or run.status != "answered" or not ledger.can_generate():
                break
            rounds += 1
            _emit(listener, "activity", {"detail": "Sending Local Coder's check results back to the model."})
            run = session.ask(
                _check_feedback(meta), max_steps=_agent_steps(ledger), deadline=time.monotonic() + ledger.remaining_s(),
            )
    finally:
        meta["agent"] = {
            "status": run.status,
            "steps": run.steps,
            "tool_calls": run.tool_calls,
            "repair_rounds": rounds,
            "read": session.toolbox.read_paths[:100],
            "changed": session.toolbox.changed_paths,
            "commands": session.toolbox.commands[:50],
            "transcript": "agent_transcript.json",
            "cost_usd": session.cost_usd,
        }
        session.save(request_root / "agent_transcript.json", status=run.status)
        shutil.rmtree(request_root / "agent-tmp", ignore_errors=True)
        _save_meta(meta)


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
            raise BudgetExhausted(ledger.stop_reason() or "generation budget exhausted")
        ledger.count += 1
        started = time.perf_counter()
        try:
            return generate(prompt)
        except PromptTooLarge:
            ledger.count -= 1
            raise
        finally:
            ledger.note_generation(time.perf_counter() - started)

    return wrapped


_STOP_TEXT = {
    "same_diagnostic": "Stopped: the same diagnostic survived two applied repairs.",
    "no_progress": "Stopped: two model replies in a row made no progress.",
    "max_repairs": "Stopped after the repair limit.",
    "generation_limit": "Stopped at the generation limit.",
    "wall_budget": "Stopped: not enough time left for another model call and check.",
}


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
    stalled = 0
    last_keys: frozenset[str] | None = None
    last_outcome: ReplyOutcome | None = None
    last_raw = ""
    views: dict[str, FileView] = {}
    previous_repair: RepairState | None = None
    repair: RepairState | None = None
    verification: dict[str, Any] = {}
    needs_check = True
    while True:
        if cancel.is_set():
            _stop(meta, "cancelled")
            return
        if needs_check or repair is None:
            check_started = time.perf_counter()
            verification = _record_verification(meta, candidate, listener, start_preview_after)
            check_s = round(time.perf_counter() - check_started, 3)
            ledger.note_check(check_s)
            meta.setdefault("timings", []).append({"check_s": check_s})
            _record_check(meta, verification, check_s)
            if verification.get("ok"):
                meta.pop("repair_state", None)
                meta["stop_reason"] = "checks_passed"
                _save_meta(meta)
                return
            repair = build_repair_state(text, verification, candidate, previous=previous_repair)
            previous_repair = repair
            write_repair_state(Path(meta["baseline"]).parent, repair)
            keys = error_keys(repair)
            if last_outcome is not None and last_outcome.kind == "applied":
                same = same + 1 if keys == last_keys else 0
            last_keys = keys
            meta["repair_state"] = {
                "check": repair.check,
                "fingerprint": repair.fingerprint(),
                "error_count": len(keys),
                "hits": [{"rel": hit.rel, "line": hit.line} for hit in repair.hits],
                "active": [
                    {"rel": hit.rel, "line": hit.line, "code": hit.code, "message": hit.message[:200]}
                    for hit in repair.active
                ],
                "remaining_count": len(diagnostic_lines(repair.diagnostic)),
                "candidate_id": repair.candidate_id,
            }
            needs_check = False
        if not allow_repair or complete is None:
            _stop(meta, "not_repaired")
            return
        reason = ""
        if same >= 2:
            reason = "same_diagnostic"
        elif stalled >= 2:
            reason = "no_progress"
        elif repairs >= MAX_CHECK_REPAIRS:
            reason = "max_repairs"
        elif not ledger.can_generate():
            reason = ledger.stop_reason() or "generation_limit"
        if reason:
            verification["stopped_incomplete"] = True
            meta["verification"] = verification
            write_verification(Path(meta["baseline"]).parent, verification)
            _stop(meta, reason)
            _emit(listener, "budget", {"detail": f"{_STOP_TEXT[reason]} The original project is unchanged."})
            return
        _emit(listener, "activity", {"detail": "Sending the check error back to the model."})
        written, last_raw, _problems, outcome = _apply_direct(
            engine_cwd, text, inspection, meta, listener, complete=complete,
            conversation=conversation, previous=last_raw or None,
            repair=repair, cancel=cancel, views=views, previous_outcome=last_outcome,
        )
        last_outcome = outcome
        if cancel.is_set() or outcome.kind == "cancelled":
            _stop(meta, "cancelled")
            return
        if outcome.kind == "applied" and written:
            invalidate_views(views, written)
            _publish_candidate(engine_cwd, candidate)
            repairs += 1
            stalled = 0
            needs_check = True
            continue
        if outcome.kind == "budget":
            reason = ledger.stop_reason() or "generation_limit"
            _stop(meta, reason)
            _emit(listener, "budget", {"detail": f"{_STOP_TEXT[reason]} The original project is unchanged."})
            return
        if outcome.kind != "read":
            stalled += 1


def _stop(meta: dict[str, Any], reason: str) -> None:
    meta["stopped_incomplete"] = True
    meta["stop_reason"] = reason
    _save_meta(meta)


def _record_check(meta: dict[str, Any], verification: dict[str, Any], check_s: float) -> None:
    """Keep every check, not only the last one, with the preview it looked at."""
    from local_coder.general.preview import read_preview

    preview = read_preview() or {}
    meta.setdefault("checks", []).append({
        "at_s": round(time.time() - float(meta.get("created_ts") or time.time()), 2),
        "check_s": check_s,
        "ok": verification.get("ok"),
        "build": verification.get("build"),
        "preview": verification.get("preview"),
        "page_mounted": verification.get("page_mounted"),
        "preview_url": preview.get("url"),
        "preview_dir": preview.get("dir"),
        "preview_pid": preview.get("pid"),
        "errors": diagnostic_lines(str(verification.get("detail") or ""))[:40],
    })
    _save_meta(meta)


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


def _title_line(text: str) -> str:
    """The first non-empty line of a request, for lists."""
    line = next((row.strip() for row in text.splitlines() if row.strip()), "")
    return line[:160]


def _title_from_transcript(request_root: Path) -> str:
    """A title for requests saved before titles were kept: the user message that started the agent."""
    try:
        data = json.loads((request_root / "agent_transcript.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return ""
    request = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            break
        if message.get("role") == "user":
            request = str(message.get("content") or "")
    return _title_line(request)


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
    views: dict[str, FileView] | None = None,
    previous_outcome: ReplyOutcome | None = None,
) -> tuple[list[str], str, list[str], ReplyOutcome]:
    """Write model file bodies onto the working copy and record what happened to each reply."""
    _emit(listener, "activity", {"detail": "Asking the model to write the files now."})
    apply_started = time.perf_counter()
    request_root = Path(meta["baseline"]).parent
    outcomes: list[ReplyOutcome] = []
    turns: list[int] = []

    def on_event(kind: str, detail: str) -> None:
        if kind == "read":
            _emit(listener, "activity", {"detail": f"Reading {detail}."})
        elif kind == "edit":
            _emit(listener, "activity", {"detail": f"Edited {detail}."})
        elif kind == "completion":
            meta.setdefault("completions", []).append(detail)
        elif kind == "prompt":
            turns.append(_write_turn(request_root, meta, "prompt", detail, advance=True))
        elif kind == "reply":
            _write_turn(request_root, meta, "reply", detail, advance=False)

    try:
        written, raw, problems = complete_files(
            engine_cwd, text, inspection, complete=complete, previous=previous,
            conversation=conversation, retry_reason=retry_reason, repair=repair,
            on_event=on_event, cancel=cancel, outcomes=outcomes, views=views,
            previous_outcome=previous_outcome,
        )
    except (RuntimeError, OSError) as exc:
        failed = ReplyOutcome("error", problems=[str(exc)])
        _record_replies(meta, [failed], turns)
        _save_meta(meta)
        _emit(listener, "activity", {"detail": f"The model call failed: {exc}"})
        return [], "", [str(exc)], failed
    _write_last_reply(request_root, raw)
    _record_replies(meta, outcomes, turns)
    meta.setdefault("timings", []).append({
        "apply_s": round(time.perf_counter() - apply_started, 3),
        "written": written,
    })
    outcome = outcomes[-1] if outcomes else ReplyOutcome("no_action")
    if outcome.kind == "applied" and written:
        meta["model_commentary"] = reply_prose(raw) or fallback_explanation(written)
        _save_meta(meta)
        _emit(listener, "activity", {"detail": f"Wrote {written[0]}."})
        return written, raw, [], outcome
    _save_meta(meta)
    _emit(listener, "activity", {"detail": _OUTCOME_TEXT.get(outcome.kind, "The model reply was not applied.")})
    return [], raw, problems, outcome


_OUTCOME_TEXT = {
    "truncated": "The model reply was cut off at the output limit and was not applied.",
    "rejected": "The model's change did not apply to the current files, so nothing changed.",
    "repeat_read": "The model asked for files already accounted for in this request.",
    "read": "Read the requested files for the model.",
    "no_action": "The model replied without file blocks.",
    "budget": "No generation budget left for another model call.",
    "cancelled": "Stopped before files were applied.",
    "error": "The model call failed.",
}


def _write_turn(request_root: Path, meta: dict[str, Any], kind: str, text: str, *, advance: bool) -> int:
    """Store each prompt and raw reply of this request under turns/, numbered by model call."""
    if advance:
        meta["turn_seq"] = int(meta.get("turn_seq") or 0) + 1
    seq = int(meta.get("turn_seq") or 0)
    folder = request_root / "turns"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{seq:02d}-{kind}.txt").write_text(text or "", encoding="utf-8")
    return seq


def _record_replies(meta: dict[str, Any], outcomes: list[ReplyOutcome], turns: list[int]) -> None:
    pending = list(turns)
    rows = meta.setdefault("replies", [])
    for outcome in outcomes:
        row = asdict(outcome)
        row["turn"] = pending.pop(0) if outcome.completion and pending else None
        rows.append(row)


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


DISCARDED_SUMMARY = "Discarded. These changes were not added to your project."
STOPPED_NOTHING = "Stopped. Nothing was changed."
STOPPED_WITH_FILES = "Stopped. Review what changed so far, or discard it."


def stopped_text(has_files: bool) -> str:
    """What a Stop says: whether anything was written decides the second sentence."""
    return STOPPED_WITH_FILES if has_files else STOPPED_NOTHING
KEPT_SUMMARY = "These changes are in your project."


def _discarded_payload(meta: dict[str, Any], project_id: str, request_id: str) -> dict[str, Any]:
    """A discarded request is closed: nothing to keep or discard, no files and no diff. Its evidence stays on disk."""
    engine = str(meta.get("engine") or "local")
    agent = meta.get("agent") if isinstance(meta.get("agent"), dict) else {}
    return {
        "kind": "code",
        "engine": engine,
        "request_id": request_id,
        "episode_id": request_id,
        "preview": {},
        "project_id": project_id,
        "repo_path": meta["user_path"],
        "summary": DISCARDED_SUMMARY,
        "diff": "",
        "outcome": DISCARDED,
        "facts": {
            "files": [],
            "delta": Delta(added=[], modified=[], deleted=[], renamed=[]).as_dict(),
            "complete": False,
            "engine": engine,
            "model_id": meta.get("model_id"),
            "provider": meta.get("provider") or "local",
            "pin": meta.get("pin") or pin_record(),
            "outcome": DISCARDED,
        },
        "can_keep": False,
        "can_discard": False,
        "verification": {
            "build": "not_run",
            "typecheck": "not_run",
            "tests": "not_run",
            "browser": "not_run",
            "preview": "not_run",
            "page_mounted": False,
            "behavior_verified": False,
            "stopped_incomplete": False,
            "files_written": False,
        },
        "model_commentary": None,
        "accept_requires_confirmation": True,
        "cost_usd": agent.get("cost_usd"),
    }


def review_payload(project_id: str, request_id: str) -> dict[str, Any]:
    meta = load_meta(project_id, request_id)
    state = request_state(meta)
    if state == DISCARDED:
        return _discarded_payload(meta, project_id, request_id)
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
    elif commentary and meta.get("engine") == "agent":
        summary = commentary
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
    if meta.get("stop_reason") == "cancelled":
        # A Stop stays a Stop, whatever the model or a half-run check said before it.
        commentary = stopped_text(bool(files))
        summary = commentary
    if state == KEPT:
        # What the model said before Keep ("the original project is unchanged...") is no longer true.
        summary = KEPT_SUMMARY
        commentary = None
    engine = str(meta.get("engine") or "local")
    return {
        "kind": "code",
        "engine": engine,
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
            "engine": engine,
            "model_id": meta.get("model_id"),
            "provider": meta.get("provider") or "local",
            "pin": meta.get("pin") or pin_record(),
            "outcome": request_outcome(meta),
        },
        "can_keep": state == WAITING and bool(files),
        "can_discard": state == WAITING and bool(files),
        "outcome": request_outcome(meta),
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
        "cost_usd": (meta.get("agent") or {}).get("cost_usd") if isinstance(meta.get("agent"), dict) else None,
    }


def keep_request(project_id: str, request_id: str, user_path: str | Path) -> dict[str, Any]:
    """Apply B→C onto the current user tree. Not an OpenCode revert."""
    meta = load_meta(project_id, request_id)
    state = request_state(meta)
    if state == DISCARDED:
        raise GeneralError("This request was discarded, so there is nothing to keep.", code="discarded")
    if state == KEPT:
        raise GeneralError("These changes are already in your project.", code="kept")
    problem = folder_problem(user_path)
    if problem:
        # Keep must never make the folder again, and must not blame "changed files" for a missing one.
        name = Path(user_path).expanduser().resolve().name
        if problem["verdict"] == "missing":
            raise GeneralError(
                f"The folder “{name}” is gone, so there is nowhere to keep these changes.",
                code="folder_gone",
            )
        raise GeneralError(
            f"“{name}” is a file, not a folder, so there is nowhere to keep these changes.",
            code=problem["verdict"],
        )
    result = apply_delta(Path(user_path), Path(meta["baseline"]), Path(meta["candidate"]))
    if not result.get("ok"):
        raise GeneralError(str(result.get("detail") or "Keep failed."), code=str(result.get("code") or "accept"))
    stop_owned()
    meta["kept"] = True
    _save_meta(meta)
    return {"ok": True, **result, "request_id": request_id, "repo": str(Path(user_path).resolve())}


def _reset_live_copy(meta: dict[str, Any]) -> None:
    """Put the project's sticky working copy back to this request's baseline.

    Without this the next request would start from the discarded work, and its diff would show it
    again. Only source files are touched: node_modules and other excluded folders stay.
    """
    live = Path(meta.get("engine_cwd") or project_dir(str(meta["project_id"])) / "live" / "candidate")
    baseline = Path(meta["baseline"])
    if live.is_dir() and baseline.is_dir():
        sync_source_tree(baseline, live)


def discard_request(project_id: str, request_id: str) -> dict[str, Any]:
    """Close the request without keeping it. Evidence (meta.json, candidate/) stays on disk."""
    meta = load_meta(project_id, request_id)
    state = request_state(meta)
    if state == KEPT:
        raise GeneralError(
            "These changes are already in your project, so they cannot be discarded.", code="kept",
        )
    if state == DISCARDED:
        return {"ok": True, "discarded": True, "request_id": request_id, "detail": "Already discarded."}
    if _request_in_flight(project_id):
        raise GeneralError(
            "A request is still running in this project. Wait for it to finish, or stop it, then discard.",
            code="busy",
        )
    try:
        _reset_live_copy(meta)
    except OSError as exc:
        raise GeneralError(
            "Local Coder could not clear this request's working copy, so it was not discarded. Try again.",
            code="discard_failed",
        ) from exc
    stop_owned()
    stop_preview()
    meta["discarded"] = True
    _save_meta(meta)
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
    root = project_dir(pid) / "requests"
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
            "outcome": request_outcome(meta),
            "request": (meta.get("title") or _title_from_transcript(entry))[:160],
            "updated_ts": meta.get("created_ts"),
            "created_ts": meta.get("created_ts"),
            "complete": False,
        })
    rows.sort(key=lambda row: row.get("created_ts") or 0, reverse=True)
    return rows
