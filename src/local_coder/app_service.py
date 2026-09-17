"""Structured application API used by the Mac app helper.

The UI talks to these functions. They reuse the controller, episode store,
server lifecycle, and accept/discard implementation. They never parse CLI
text and never start a hidden second episode to evade a budget.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from . import accept as accept_mod
from .config import CONFIG_FILENAME, EXAMPLE_TOML, RepoConfig, load_config
from .controller import Controller, git_is_dirty, git_revision
from .execution.isolated import IsolatedBackend, IsolationUnavailable
from .hostenv import apply_host_path
from .machine import (
    config_dir,
    config_path,
    load_machine_config,
    machine_status,
    write_machine_config,
)
from .persistence import EpisodeStore, new_episode_id
from .profiles import DEFAULT_PROFILE, profile_identity
from .progress import latest_progress, phrase_for_event
from .runtime import (
    DEFAULT_STATE_DIR,
    build_adapter,
    build_backend,
    effective_protocol,
    effective_verification,
    episode_store,
    isolation_status,
    selected_profile,
)
from .server import ServerError, server_start, server_status, server_stop
from .skills.find import run_find

APP_PREFS_NAME = "app.json"
_WEB_HINT = re.compile(
    r"\b(react|jsx|tsx|typescript|javascript|vue\.?js|svelte|angular|html|css|next\.js)\b",
    re.I,
)
_MOBILE_HINT = re.compile(r"\b(swiftui|uikit|android|kotlin|xcode project)\b", re.I)
_UI_WIDGET_HINT = re.compile(
    r"\b(search box|login page|landing page|website|web app|frontend|css layout)\b",
    re.I,
)
_EXPLAIN_HINT = re.compile(
    r"^(explain|what does|what is|how does|how do|describe|show me|walk me through|summarize)\b",
    re.I,
)
_PYTHON_WEB = re.compile(r"\b(flask|django|fastapi|jinja)\b", re.I)

EventListener = Callable[[dict[str, Any]], None]


class AppError(Exception):
    """A refused application operation with a user-facing detail."""

    def __init__(self, detail: str, code: str = "refused") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


class NotifyingStore(EpisodeStore):
    """Episode store that forwards each appended event to a listener."""

    def __init__(self, state_dir: Path, listener: EventListener | None = None) -> None:
        super().__init__(state_dir)
        self.listener = listener

    def append_event(self, episode_id: str, event_type: str, data: dict | None = None) -> int:
        """Append one event and notify the UI listener."""
        seq = super().append_event(episode_id, event_type, data)
        if self.listener is not None:
            event = {"seq": seq, "type": event_type, "data": data or {}}
            self.listener(event)
        return seq


def prefs_path() -> Path:
    """Machine-local application preferences (not the model config)."""
    return config_dir() / APP_PREFS_NAME


def load_prefs() -> dict[str, Any]:
    """Load recent projects and UI preferences."""
    path = prefs_path()
    if not path.exists():
        return {"recent_projects": [], "profile": DEFAULT_PROFILE, "engine": "general"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"recent_projects": [], "profile": DEFAULT_PROFILE, "engine": "general"}
    if not isinstance(data, dict):
        return {"recent_projects": [], "profile": DEFAULT_PROFILE, "engine": "general"}
    data.setdefault("recent_projects", [])
    data.setdefault("profile", DEFAULT_PROFILE)
    data.setdefault("engine", "general")
    return data


def save_prefs(prefs: dict[str, Any]) -> Path:
    """Persist application preferences atomically."""
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def remember_project(repo: str | Path) -> dict[str, Any]:
    """Record a project on the recent list without exposing model settings."""
    prefs = load_prefs()
    folder = Path(repo).expanduser().resolve()
    resolved = str(folder)
    recent = [row for row in prefs.get("recent_projects", []) if row.get("path") != resolved]
    recent.insert(0, {"path": resolved, "name": folder.name})
    prefs["recent_projects"] = recent[:12]
    save_prefs(prefs)
    return prefs


def _count_by_suffix(root: Path, suffixes: tuple[str, ...], limit: int = 400) -> int:
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
               for part in path.parts):
            continue
        if path.suffix.lower() in suffixes:
            count += 1
            if count >= limit:
                return count
    return count


def inspect_legacy_project(path: str | Path) -> dict[str, Any]:
    """Historical Python/pytest admission used only by the legacy engine."""
    repo = Path(path).expanduser().resolve()
    report: dict[str, Any] = {
        "path": str(repo),
        "name": repo.name,
        "exists": repo.is_dir(),
        "supported": False,
        "verdict": "missing",
        "summary": "",
        "issues": [],
        "python_files": 0,
        "test_files": 0,
        "has_pytest": False,
        "has_config": False,
        "execution": None,
        "git": {"present": False, "dirty": False, "revision": None},
        "languages": [],
        "notes": [],
    }
    if not repo.is_dir():
        report["summary"] = "That folder does not exist."
        report["issues"].append({"code": "missing", "message": report["summary"], "blocking": True})
        return report
    python_files = _count_by_suffix(repo, (".py",))
    test_files = len(list((repo / "tests").glob("test_*.py"))) if (repo / "tests").is_dir() else 0
    js_files = _count_by_suffix(repo, (".js", ".jsx", ".ts", ".tsx", ".vue"))
    report["python_files"] = python_files
    report["test_files"] = test_files
    languages = []
    if python_files:
        languages.append("Python")
    if js_files:
        languages.append("JavaScript/TypeScript")
    report["languages"] = languages
    pyproject = repo / "pyproject.toml"
    has_pytest_ini = (repo / "pytest.ini").exists() or (repo / "conftest.py").exists()
    has_pytest_project = False
    if pyproject.exists():
        has_pytest_project = "pytest" in pyproject.read_text(encoding="utf-8", errors="ignore")
    report["has_pytest"] = has_pytest_ini or test_files > 0 or has_pytest_project
    config_file = repo / CONFIG_FILENAME
    report["has_config"] = config_file.exists()
    if config_file.exists():
        try:
            config = load_config(repo)
            report["execution"] = config.execution
        except ValueError as exc:
            report["issues"].append({
                "code": "bad_config", "message": f"Project config is invalid: {exc}", "blocking": True,
            })
    if (repo / ".git").exists():
        report["git"] = {
            "present": True,
            "dirty": git_is_dirty(repo),
            "revision": git_revision(repo)[:12],
        }
    else:
        report["notes"].append("This folder is not a git checkout. Changes are still kept in a copy until you accept.")

    if js_files >= 8 and python_files < 3:
        report["verdict"] = "unsupported"
        report["summary"] = (
            "This looks like a JavaScript/TypeScript project. Local Coder currently "
            "edits small, testable Python projects."
        )
        report["issues"].append({"code": "language", "message": report["summary"], "blocking": True})
        return report
    if python_files == 0:
        report["verdict"] = "unsupported"
        report["summary"] = "No Python files were found. This version only changes existing Python files."
        report["issues"].append({"code": "no_python", "message": report["summary"], "blocking": True})
        return report
    if test_files == 0 and not report["has_pytest"]:
        report["verdict"] = "unsupported"
        report["summary"] = (
            "No pytest tests were found. This version only works on small Python "
            "projects it can check with tests."
        )
        report["issues"].append({"code": "no_tests", "message": report["summary"], "blocking": True})
        return report
    if report["git"]["dirty"]:
        report["verdict"] = "dirty"
        report["summary"] = (
            "This project has uncommitted changes. Local Coder will not start an "
            "edit until the folder is clean, so it cannot mix its work with yours."
        )
        report["issues"].append({"code": "dirty", "message": report["summary"], "blocking": True})
        report["supported"] = False
        return report
    if report.get("execution") == "trusted-local":
        report["notes"].append(
            "This project is configured to run tests directly on your Mac "
            "(not in the isolated worker). That is an explicit project setting."
        )
    report["supported"] = True
    report["verdict"] = "ready"
    report["summary"] = (
        "This is a Python project with tests. You can describe a small change "
        "in ordinary language. The original files stay untouched until you keep a result."
    )
    return report


def inspect_project(path: str | Path) -> dict[str, Any]:
    """Default product inspect: general application development."""
    from local_coder.general.inspect import inspect_project as general_inspect

    return general_inspect(path)


def classify_legacy_request(text: str, inspection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Historical Python-only classifier used only by the legacy engine."""
    request = text.strip()
    if not request:
        return {"kind": "unsupported", "summary": "Write what you would like to change.",
                "detail": "The request box is empty."}
    if _EXPLAIN_HINT.search(request):
        return {"kind": "explain", "summary": "This will look up the selected code and stay read-only.",
                "detail": "No files will be edited."}
    if _MOBILE_HINT.search(request):
        return {"kind": "unsupported",
                "summary": "This version does not edit iOS or Android apps.",
                "detail": "The local engine changes existing Python files that tests can check."}
    if _WEB_HINT.search(request) or _UI_WIDGET_HINT.search(request):
        blob = ""
        if inspection:
            blob = " ".join(str(x) for x in inspection.get("languages") or [])
        if not _PYTHON_WEB.search(blob) and "Python" in (inspection or {}).get("languages", ["Python"]):
            return {"kind": "unsupported",
                    "summary": "That sounds like a web or UI change this engine cannot do.",
                    "detail": "Local Coder currently repairs small, testable Python behavior, not front-end widgets."}
    if inspection and not inspection.get("supported"):
        return {"kind": "unsupported",
                "summary": inspection.get("summary") or "This project is not ready.",
                "detail": inspection.get("verdict")}
    return {"kind": "repair", "summary": "This will try a bounded local coding attempt.",
            "detail": "You will review any proposed change before it is kept."}


def classify_request(text: str, inspection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Default product classifier: new apps and web work are allowed."""
    from local_coder.general.inspect import classify_request as general_classify

    return general_classify(text, inspection)


def explain_request(path: str | Path, text: str) -> dict[str, Any]:
    """Read-only, retrieval-grounded explanation. No PATCH episode, no cloud."""
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise AppError("That folder does not exist.")
    config = load_config(repo)
    from .retrieval.inventory import Inventory

    inventory = Inventory.build(repo, config.retrieval_allow, config.retrieval_deny)
    hits, _focus, observation = run_find(
        repo, inventory, text, [], [], text, config.budgets.max_focus_files, [],
    )
    excerpts = [{
        "path": hit.path,
        "start": hit.start,
        "end": hit.end,
        "excerpt": hit.excerpt,
        "reasons": hit.reasons[:3],
    } for hit in hits[:6]]
    if not excerpts:
        summary = "I did not find matching code in this project for that question."
    else:
        names = ", ".join(sorted({str(row["path"]) for row in excerpts}))
        summary = f"These project files match the question: {names}. The excerpts below are the actual source."
    return {
        "kind": "explain",
        "summary": summary,
        "hits": excerpts,
        "status": observation.get("status"),
        "model_commentary": None,
    }


def prepare_project(path: str | Path, *, write_config: bool = False) -> dict[str, Any]:
    """Optionally write repo config after an explicit confirmation."""
    repo = Path(path).expanduser().resolve()
    inspection = inspect_project(repo)
    if not write_config:
        return {"wrote": False, "inspection": inspection,
                "detail": "Using engine defaults when no project config file exists."}
    if not inspection["exists"]:
        raise AppError("That folder does not exist.")
    dest = repo / CONFIG_FILENAME
    if dest.exists():
        return {"wrote": False, "inspection": inspect_project(repo),
                "detail": f"{CONFIG_FILENAME} already exists."}
    dest.write_text(EXAMPLE_TOML.replace('execution = "trusted-local"', 'execution = "isolated"'),
                    encoding="utf-8")
    written = dest
    return {"wrote": True, "path": str(written), "inspection": inspect_project(repo),
            "detail": (
                f"Wrote {CONFIG_FILENAME}. If this is a git checkout, commit that file "
                "yourself before starting a request — the app will not commit it."
            )}


def ensure_machine_config() -> dict[str, Any]:
    """Write machine config only when missing, using detected local defaults."""
    path = config_path()
    if path.exists():
        cfg = load_machine_config()
        return {"wrote": False, "path": str(path), "config": cfg.model_dump()}
    cfg = load_machine_config()
    written = write_machine_config(cfg)
    return {"wrote": True, "path": str(written), "config": cfg.model_dump()}


def readiness() -> dict[str, Any]:
    """Prerequisites for ordinary app use. No model generation."""
    apply_host_path()
    cfg = load_machine_config()
    rows = []
    blocking = []
    for name, ok, detail in machine_status(cfg):
        rows.append({"name": name, "ok": ok, "detail": detail})
        if not ok and name != "endpoint":
            blocking.append(detail)
    status = server_status(cfg)
    rows.append({
        "name": "model server",
        "ok": bool(status["healthy"] and status["owned"]),
        "detail": (
            f"healthy owned server at {cfg.endpoint}" if status["healthy"] and status["owned"]
            else status.get("note") or f"not running at {cfg.endpoint}"
        ),
    })
    isolation = isolation_status()
    rows.append({"name": "isolated tests", "ok": isolation["ok"], "detail": isolation["detail"]})
    if not isolation["ok"]:
        blocking.append(str(isolation["detail"]))
    docker_app = Path("/Applications/Docker.app").exists()
    return {
        "ok": not blocking,
        "rows": rows,
        "blocking": blocking,
        "server": status,
        "isolation": isolation,
        "docker_app_present": docker_app,
        "config_path": str(config_path()),
        "version": __version__,
    }


def ensure_server(*, wait_s: int = 90) -> dict[str, Any]:
    """Start the owned local model if needed. Never adopt a foreign server."""
    apply_host_path()
    cfg = load_machine_config()
    status = server_status(cfg)
    if status["healthy"] and status["owned"]:
        return {"ok": True, "started": False, "status": status}
    if status["healthy"] and not status["owned"]:
        raise AppError(
            f"A model server is already running at {cfg.endpoint} but Local Coder "
            "did not start it. Stop that server yourself, or the app will not take it over.",
            code="foreign_server",
        )
    try:
        result = server_start(cfg, wait_s=wait_s)
    except ServerError as exc:
        raise AppError(str(exc), code="server") from exc
    return {"ok": True, "started": True, "status": server_status(cfg), "start": result}


def stop_owned_server() -> dict[str, Any]:
    """Stop only a server this tool started. Missing ownership is not a crash."""
    try:
        result = server_stop()
        return {"ok": True, **result}
    except ServerError as exc:
        return {"ok": True, "stopped": False, "detail": str(exc)}


def open_docker_desktop() -> dict[str, Any]:
    """Ask macOS to open Docker Desktop. Does not wait for the daemon."""
    app = Path("/Applications/Docker.app")
    if not app.exists():
        raise AppError("Docker Desktop is not installed in /Applications.")
    subprocess.Popen(["/usr/bin/open", str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "detail": "Opening Docker Desktop. Wait until it is running, then try again."}


def build_worker_image(docker_dir: str | Path) -> dict[str, Any]:
    """Build the isolated worker from the bundled Dockerfile after confirmation."""
    apply_host_path()
    directory = Path(docker_dir).expanduser().resolve()
    dockerfile = directory / "worker.Dockerfile"
    if not dockerfile.exists():
        raise AppError(f"Worker Dockerfile not found: {dockerfile}")
    image = IsolatedBackend.default_image
    proc = subprocess.run(
        ["docker", "build", "-t", image, "-f", str(dockerfile), str(directory)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise AppError(f"Could not build the test worker: {proc.stderr.strip()[:400]}")
    return {"ok": True, "image": image, "detail": f"Built {image}."}


def _check_summary(evidence: Any) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "scope": evidence.scope,
        "status": evidence.status,
        "failed": list(evidence.failed),
        "errors": list(evidence.errors),
        "passed_count": len(evidence.passed),
        "failure_summary": evidence.failure_summary,
        "incomplete_reason": evidence.incomplete_reason,
    }


def _read_diff(store: EpisodeStore, episode_id: str) -> str:
    try:
        return store.read_artifact_text(episode_id, "artifacts/final.diff")
    except OSError:
        return ""


def snapshot(episode_id: str, store: EpisodeStore | None = None,
             repo: str | Path | None = None) -> dict[str, Any]:
    """Controller-owned review payload for the UI."""
    if repo is not None:
        from local_coder.general.coordinator import project_id_for, request_dir, review_payload

        meta = request_dir(project_id_for(Path(repo)), episode_id) / "meta.json"
        if meta.exists():
            return review_payload(project_id_for(Path(repo)), episode_id)
    store = store or episode_store()
    state = store.load_state(episode_id)
    events = store.read_events(episode_id)
    files = list(state.last_patch.files) if state.last_patch else []
    complete = state.outcome == "solved" and state.final is not None and state.final.status == "pass"
    model_note = None
    if state.outcome == "solved" and state.stop_reason:
        model_note = state.stop_reason
    facts = {
        "outcome": state.outcome,
        "stop_reason": state.stop_reason,
        "phase": state.phase,
        "generations_used": state.generations_used,
        "generation_budget": state.budgets.max_generations,
        "files": files,
        "complete": complete,
        "baseline": _check_summary(state.baseline),
        "current": _check_summary(state.current),
        "final": _check_summary(state.final),
        "accept_allowed": complete,
    }
    if state.outcome == "solved" and complete:
        summary = "The local checks passed for this complete change. Review the diff before keeping it."
    elif state.outcome == "blocked":
        summary = f"Stopped without a verified complete change. {state.stop_reason or ''}".strip()
    elif state.outcome == "discarded":
        summary = "This request was discarded. The original project was not changed."
    elif state.outcome == "running":
        summary = latest_progress(events)["phrase"]
    else:
        summary = state.stop_reason or state.outcome
    return {
        "episode_id": episode_id,
        "repo_path": state.repo_path,
        "ticket": state.ticket,
        "summary": summary,
        "facts": facts,
        "model_commentary": model_note if model_note and model_note != summary else None,
        "diff": _read_diff(store, episode_id),
        "progress": latest_progress(events),
        "events": [{"seq": e["seq"], "type": e["type"], "data": e.get("data") or {}} for e in events],
        "can_keep": bool(facts["accept_allowed"]),
        "can_discard": state.outcome in {"solved", "blocked", "running"} and store.candidate_dir(episode_id).exists(),
    }


def list_history(repo: str | Path, store: EpisodeStore | None = None) -> list[dict[str, Any]]:
    """Completed or blocked requests for one project, newest first."""
    from local_coder.general.coordinator import list_requests

    rows = list_requests(repo)
    store = store or episode_store()
    resolved = str(Path(repo).expanduser().resolve())
    if not store.state_dir.exists():
        return rows
    for entry in store.state_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        try:
            state = store.load_state(entry.name)
        except (FileNotFoundError, ValueError, OSError):
            continue
        if Path(state.repo_path).resolve() != Path(resolved):
            continue
        rows.append({
            "episode_id": state.episode_id,
            "outcome": state.outcome,
            "stop_reason": state.stop_reason,
            "request": state.ticket.strip().splitlines()[0][:160] if state.ticket.strip() else "",
            "updated_ts": state.updated_ts,
            "created_ts": state.created_ts,
            "complete": state.outcome == "solved",
        })
    rows.sort(key=lambda row: row.get("updated_ts") or "", reverse=True)
    return rows


def _active_lock_path(repo: Path) -> Path:
    digest = re.sub(r"[^a-zA-Z0-9]+", "_", str(repo.resolve()))[:80]
    return config_dir() / "locks" / f"{digest}.json"


def _project_busy(repo: Path) -> str | None:
    path = _active_lock_path(repo)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    return str(data.get("episode_id") or "unknown")


def _lock_project(repo: Path, episode_id: str) -> None:
    path = _active_lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"episode_id": episode_id, "repo": str(repo)}), encoding="utf-8")


def _unlock_project(repo: Path) -> None:
    _active_lock_path(repo).unlink(missing_ok=True)


def _pin_registered_python(config: RepoConfig) -> RepoConfig:
    """Use this process's interpreter for python3 test commands.

    Finder-launched apps often have a generic `python3` on PATH that does not
    include pytest. The installed engine already depends on pytest, so the
    helper interpreter is the one that can actually run checks.
    """
    pinned: dict[str, list[str]] = {}
    for scope, argv in config.commands.items():
        if argv and Path(argv[0]).name in {"python", "python3"}:
            pinned[scope] = [sys.executable, *argv[1:]]
        else:
            pinned[scope] = list(argv)
    return config.model_copy(update={"commands": pinned})


def run_request(
    repo: str | Path,
    request: str,
    *,
    profile_name: str = DEFAULT_PROFILE,
    execution: str | None = None,
    state_dir: Path | None = None,
    episode_id: str | None = None,
    listener: EventListener | None = None,
    cancel_event: threading.Event | None = None,
    ensure_local_server: bool = True,
) -> dict[str, Any]:
    """Run one bounded episode. Original files are not modified."""
    apply_host_path()
    folder = Path(repo).expanduser().resolve()
    inspection = inspect_legacy_project(folder)
    classified = classify_legacy_request(request, inspection)
    if classified["kind"] == "explain":
        return {"kind": "explain", **explain_request(folder, request)}
    if classified["kind"] != "repair":
        raise AppError(classified["summary"], code="unsupported")
    if not inspection["supported"]:
        raise AppError(inspection["summary"], code=inspection["verdict"])
    busy = _project_busy(folder)
    if busy:
        raise AppError(
            f"A request is already running for this project ({busy}). "
            "Wait for it to finish or stop it. A new run will not be started to bypass that.",
            code="busy",
        )
    remember_project(folder)
    store = NotifyingStore(episode_store(state_dir).state_dir, listener)
    config = _pin_registered_python(load_config(folder))
    if ensure_local_server and not str(profile_name).startswith("fake:"):
        ensure_server()
        ensure_machine_config()
    try:
        adapter = build_adapter(profile_name)
        backend = build_backend(config, execution)
    except IsolationUnavailable as exc:
        raise AppError(
            f"Isolated tests are not available: {exc}. "
            "The app will not run tests directly on your Mac unless this project "
            "already chose that explicitly.",
            code="isolation",
        ) from exc
    except Exception as exc:
        raise AppError(str(exc), code="setup") from exc
    profile = selected_profile(profile_name)
    episode = episode_id or new_episode_id()
    _lock_project(folder, episode)
    controller = Controller(
        store, config, adapter, backend, folder, request.strip(), episode_id=episode,
        protocol_version=effective_protocol(profile),
        verification_policy=effective_verification(profile),
        profile_identity=profile_identity(profile) if profile else None,
        cancel_event=cancel_event,
    )
    try:
        controller.run()
    except RuntimeError as exc:
        raise AppError(str(exc), code="run") from exc
    finally:
        _unlock_project(folder)
    return snapshot(episode, store)


def keep_changes(episode_id: str, repo: str | Path, store: EpisodeStore | None = None) -> dict[str, Any]:
    """Accept a general request or a legacy episode."""
    from local_coder.general.coordinator import keep_request, project_id_for, request_dir

    folder = Path(repo)
    meta = request_dir(project_id_for(folder), episode_id) / "meta.json"
    if meta.exists():
        return keep_request(project_id_for(folder), episode_id, folder)
    store = store or episode_store()
    result = accept_mod.accept_episode(store, episode_id, Path(repo))
    if not result["ok"]:
        raise AppError(str(result["detail"]), code="accept")
    return result


def discard_changes(episode_id: str, store: EpisodeStore | None = None,
                    repo: str | Path | None = None) -> dict[str, Any]:
    """Discard the candidate. Evidence stays."""
    if repo is not None:
        from local_coder.general.coordinator import discard_request, project_id_for, request_dir

        meta = request_dir(project_id_for(Path(repo)), episode_id) / "meta.json"
        if meta.exists():
            return discard_request(project_id_for(Path(repo)), episode_id)
    store = store or episode_store()
    return accept_mod.discard_episode(store, episode_id)


class AppRuntime:
    """One helper process: at most one active editing episode."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        self.cancel_event = threading.Event()
        self.started_server = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.active_episode: str | None = None
        self.active_repo: str | None = None
        self.last_error: str | None = None
        self._listeners: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        """Add a progress listener (RPC event stream)."""
        self._listeners.append(listener)

    def _emit(self, event: dict[str, Any]) -> None:
        progress = phrase_for_event(str(event.get("type", "")), event.get("data") or {})
        payload = {**event, "phrase": progress}
        for listener in list(self._listeners):
            listener(payload)

    def start_request(self, repo: str, request: str, profile_name: str = DEFAULT_PROFILE,
                      execution: str | None = None, engine: str | None = None,
                      kind: str = "create") -> dict[str, Any]:
        """Start a request on a worker thread and return immediately."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise AppError("A request is already running.", code="busy")
            self.cancel_event.clear()
            self.last_error = None
            self.active_episode = new_episode_id()
            self.active_repo = str(Path(repo).expanduser().resolve())
            episode = self.active_episode
            chosen = engine or load_prefs().get("engine") or "general"
            thread = threading.Thread(
                target=self._worker,
                kwargs={
                    "repo": repo,
                    "request": request,
                    "profile_name": profile_name,
                    "execution": execution,
                    "episode_id": episode,
                    "engine": chosen,
                    "kind": kind,
                },
                name="local-coder-episode",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return {"started": True, "episode_id": episode, "repo": self.active_repo,
                "engine": chosen}

    def _worker(self, **kwargs: Any) -> None:
        started = False
        try:
            engine = str(kwargs.get("engine") or "general")
            profile = str(kwargs.get("profile_name") or "")
            if engine == "legacy" or profile.startswith("legacy:"):
                if not profile.startswith("fake:"):
                    result = ensure_server()
                    started = bool(result.get("started"))
                    if started:
                        self.started_server = True
                run_request(
                    kwargs["repo"], kwargs["request"],
                    profile_name=profile.removeprefix("legacy:"),
                    execution=kwargs.get("execution"),
                    state_dir=self.state_dir,
                    episode_id=kwargs["episode_id"],
                    listener=self._emit,
                    cancel_event=self.cancel_event,
                    ensure_local_server=False,
                )
                return
            from local_coder.general.coordinator import run_general_request

            run_general_request(
                kwargs["repo"], kwargs["request"],
                request_id=kwargs["episode_id"],
                kind=str(kwargs.get("kind") or "create"),
                listener=self._emit,
                cancel_event=self.cancel_event,
            )
        except AppError as exc:
            self.last_error = exc.detail
            self._emit({"type": "app_error", "data": {"detail": exc.detail, "code": exc.code},
                        "seq": None})
        except Exception as exc:  # noqa: BLE001 — surface unexpected helper failures
            self.last_error = str(exc)
            self._emit({"type": "app_error", "data": {"detail": str(exc), "code": "runtime"},
                        "seq": None})
        finally:
            with self._lock:
                self._thread = None

    def cancel(self) -> dict[str, Any]:
        """Request stop of the active episode and its OpenCode session."""
        self.cancel_event.set()
        try:
            from local_coder.general.coordinator import cancel_active

            cancel_active()
        except Exception:
            pass
        return {"ok": True, "episode_id": self.active_episode, "detail": "Stop requested."}

    def shutdown(self) -> dict[str, Any]:
        """Cancel work and stop a server this app started."""
        self.cancel()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=20)
        stopped = None
        if self.started_server:
            stopped = stop_owned_server()
        return {"ok": True, "cancelled": True, "server": stopped}

    def is_running(self) -> bool:
        """Whether a worker thread is active."""
        thread = self._thread
        return thread is not None and thread.is_alive()
