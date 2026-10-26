"""Structured application API used by the Mac app helper.

The UI talks to these functions. They reuse the controller, episode store,
server lifecycle, and accept/discard implementation. They never parse CLI
text and never start a hidden second episode to evade a budget.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from . import BUILD_ID, __version__
from . import accept as accept_mod
from .catalog import entry_by_id, list_models
from .config import CONFIG_FILENAME, EXAMPLE_TOML, RepoConfig, load_config
from .controller import Controller, git_is_dirty, git_revision
from .errorlog import UNEXPECTED, record_error
from .execution.isolated import IsolatedBackend, IsolationUnavailable
from .hosted import HostedError, find_api_key, hosted_chat, provider_place
from .hostenv import apply_host_path
from .machine import (
    DEFAULT_ENDPOINT,
    config_dir,
    config_path,
    is_hosted,
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
    data["recent_projects"] = [
        row for row in data["recent_projects"]
        if isinstance(row, dict) and Path(str(row.get("path") or "")).expanduser().is_dir()
    ]
    return data


def save_prefs(prefs: dict[str, Any]) -> Path:
    """Persist application preferences atomically."""
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def known_projects() -> list[Path]:
    """Projects a request can name: the recent list, then the folders in ~/projects and ~/Local Coder Projects."""
    from local_coder.general.starter import projects_home

    found: list[Path] = [Path(str(row.get("path"))) for row in load_prefs().get("recent_projects", [])]
    for root in (Path.home() / "projects", projects_home()):
        try:
            found.extend(sorted(child for child in root.iterdir() if child.is_dir() and not child.name.startswith(".")))
        except OSError:
            continue
    return found


def remember_project(repo: str | Path, *, active: bool = True) -> dict[str, Any]:
    """Record a project on the recent list, newest activity first.

    `active` is a request or a chat in it: its time moves it up. Opening or clicking a project only adds it when it is
    missing, so the list does not jump around under the pointer (Nico, 2026-10-26: "when i click on one it becomes the
    first in the list which is very confusing").
    """
    prefs = load_prefs()
    folder = Path(repo).expanduser().resolve()
    resolved = str(folder)
    now = time.time()
    recent = list(prefs.get("recent_projects", []))
    row = next((item for item in recent if item.get("path") == resolved), None)
    if row is None:
        row = {"path": resolved, "name": folder.name, "active": now}
        recent.append(row)
    elif active:
        row["active"] = now
    # Newest activity first; rows from before activity times keep their old order after the timed ones.
    order = {id(item): index for index, item in enumerate(recent)}
    recent.sort(key=lambda item: (-float(item.get("active") or 0), order[id(item)]))
    prefs["recent_projects"] = recent[:20]
    save_prefs(prefs)
    return prefs


def dated_prefs() -> dict[str, Any]:
    """Preferences for the app, every recent project with the time it was last used.

    Rows saved before activity times (2026-10-26) get theirs once from the project's own records: the newest chat
    turn or request, else the folder's last change. The list is then in that order, newest first.
    """
    prefs = load_prefs()
    recent = list(prefs.get("recent_projects", []))
    missing = [row for row in recent if not isinstance(row.get("active"), int | float)]
    if not missing:
        return prefs
    for row in missing:
        row["active"] = _last_used(Path(str(row["path"])).expanduser())
    order = {id(item): index for index, item in enumerate(recent)}
    recent.sort(key=lambda item: (-float(item.get("active") or 0), order[id(item)]))
    prefs["recent_projects"] = recent
    save_prefs(prefs)
    return prefs


def _last_used(folder: Path) -> float:
    from local_coder.general.chat_log import load_chat
    from local_coder.general.coordinator import project_dir, project_id_for

    times = [float(turn["ts"]) for turn in load_chat(folder)["turns"] if isinstance(turn.get("ts"), int | float)]
    for meta in (project_dir(project_id_for(folder)) / "requests").glob("*/meta.json"):
        try:
            created = json.loads(meta.read_text(encoding="utf-8")).get("created_ts")
        except (OSError, ValueError):
            continue
        if isinstance(created, int | float):
            times.append(float(created))
    if times:
        return max(times)
    try:
        return folder.stat().st_mtime
    except OSError:
        return 0.0


def start_chat(text: str, *, parent: str | Path | None = None, chat_id: str | None = None) -> dict[str, Any]:
    """Create a project from a message and remember it.

    When the message came from a chat without a project (`chat_id`), that chat moves into the new project, so
    "do it" after a discussion builds what was discussed (and names the project after it).
    """
    from local_coder.general.chat_log import load_free_chat, move_free_chat
    from local_coder.general.inspect import inspect_project as general_inspect
    from local_coder.general.starter import (
        allocate_project_name,
        create_project,
        project_name_from_request,
        projects_home,
    )

    request = text.strip()
    if not request:
        raise AppError("Write what you would like to build or change.", code="empty")
    earlier: list[dict[str, Any]] = []
    if chat_id:
        try:
            earlier = load_free_chat(chat_id)["turns"]
        except ValueError as exc:
            raise AppError("That chat does not exist.") from exc
    naming = request
    if earlier and len(request.split()) < 4:
        naming = next((turn["text"] for turn in earlier if turn["role"] == "user"), request)
    home = Path(parent).expanduser() if parent else projects_home()
    home.mkdir(parents=True, exist_ok=True)
    name = allocate_project_name(home, project_name_from_request(naming))
    dest = create_project(home, name)
    remember_project(dest)
    if earlier:
        move_free_chat(str(chat_id), dest)
    report = general_inspect(dest)
    return {
        "path": str(dest),
        "name": dest.name,
        "inspection": report,
        "detail": f"Created {dest.name} in {home}.",
        "moved_chat": bool(earlier),
    }


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
            "This looks like a JavaScript/TypeScript project. Nicoder currently "
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
            "This project has uncommitted changes. Nicoder will not start an "
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
                    "detail": "Nicoder currently repairs small, testable Python behavior, not front-end widgets."}
    if inspection and not inspection.get("supported"):
        return {"kind": "unsupported",
                "summary": inspection.get("summary") or "This project is not ready.",
                "detail": inspection.get("verdict")}
    return {"kind": "repair", "summary": "This will try a bounded local coding attempt.",
            "detail": "You will review any proposed change before it is kept."}


def classify_request(text: str, inspection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Default product classifier: new apps and web work are allowed.

    With an agent-capable model, "look" requests become questions: the agent
    reads the code itself instead of the lexical excerpt view.
    """
    from local_coder.agent.runner import agent_capable
    from local_coder.general.inspect import classify_request as general_classify

    result = general_classify(text, inspection)
    if result.get("kind") == "explain" and agent_capable(load_machine_config()):
        summary = "This will look through the code and answer without editing files."
        return {**result, "kind": "chat", "summary": summary}
    return result


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
    from local_coder.general.chat_log import append_turn

    append_turn(repo, "user", text.strip())
    transcript = append_turn(repo, "assistant", summary)
    return {
        "kind": "explain",
        "summary": summary,
        "hits": excerpts,
        "status": observation.get("status"),
        "model_commentary": None,
        "turns": transcript["turns"],
    }


def chat_request(
    path: str | Path | None,
    text: str,
    *,
    listener: EventListener | None = None,
    cancel: threading.Event | None = None,
    history: Any = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Answer an ordinary question. No canned replies.

    With an agent-capable model the agent searches and reads the Mac itself and
    may open the project it found (`open_project` in the result). `path` is
    None when no project is open yet; then the chat `chat_id` is saved on its own (chats/free/) and its
    turns are the history. Without a `chat_id`, `history` is the conversation the app shows.
    """
    from local_coder.agent.runner import agent_capable
    from local_coder.general.inspect import asked_to_see_ui

    repo = Path(path).expanduser().resolve() if path else None
    if repo is not None and not repo.is_dir():
        raise AppError("That folder does not exist.")
    question = text.strip()
    if not question:
        raise AppError("Write a question first.")
    if repo is not None:
        remember_project(repo)
    extra = ""
    preview = None
    if repo is not None and asked_to_see_ui(question):
        from local_coder.general.coordinator import change_running_elsewhere

        if change_running_elsewhere(repo):
            # One preview server: starting this one would stop the preview a running change is checking.
            extra = ("The preview was not started: a change is being made and checked in another project right "
                     "now, and it uses the preview. Say it again when that change has finished.")
        else:
            preview, extra = _start_preview_note(repo)
    with_tools = agent_capable(load_machine_config())
    waiting = _waiting_note(repo, can_read=with_tools)
    if waiting:
        extra = f"{extra}\n{waiting}".strip()
    shown = None
    if repo is None:
        shown = _free_history(chat_id) if chat_id else _shown_history(history)
    if repo is None and chat_id:
        from local_coder.general.chat_log import append_free_turn

        # Saved before the answer: a question that fails or is stopped stays in its chat.
        append_free_turn(chat_id, "user", question)
    if with_tools:
        result = _agent_chat(repo, question, extra=extra, listener=listener, cancel=cancel, history=shown)
    else:
        from local_coder.general.chat_log import recent_messages

        reply = _local_chat_completion(
            question, repo, extra=extra, history=recent_messages(repo) if repo is not None else shown,
        )
        if repo is not None:
            result = _chat_result(repo, question, reply)
        else:
            result = {"kind": "chat", "summary": reply, "hits": [], "model_commentary": reply, "status": "answered"}
    if preview is not None:
        result["preview"] = preview
    if repo is None and chat_id:
        from local_coder.general.chat_log import append_free_turn

        opened = result.get("open_project")
        reply = (f"I answered this in {Path(str(opened)).name}." if opened
                 else str(result.get("model_commentary") or result.get("summary") or ""))
        result["turns"] = append_free_turn(chat_id, "assistant", reply)["turns"]
        result["chat_id"] = chat_id
    return result


def _free_history(chat_id: str) -> list[dict[str, str]] | None:
    from local_coder.general.chat_log import recent_free_messages

    try:
        return recent_free_messages(chat_id) or None
    except ValueError as exc:
        raise AppError("That chat does not exist.") from exc


def load_free_chat_payload(chat_id: str) -> dict[str, Any]:
    """A saved chat without a project, for the app."""
    from local_coder.general.chat_log import load_free_chat

    try:
        return load_free_chat(chat_id)
    except ValueError as exc:
        raise AppError("That chat does not exist.") from exc


def list_free_chat_rows() -> dict[str, Any]:
    """Saved chats without a project, newest activity first."""
    from local_coder.general.chat_log import list_free_chats

    return {"chats": list_free_chats()}


def _shown_history(rows: Any, *, limit: int = 10, max_chars: int = 1500) -> list[dict[str, str]] | None:
    """The turns the app shows in a chat without a project, clipped the way a project's saved chat is."""
    if not isinstance(rows, list):
        return None
    clean: list[dict[str, str]] = []
    for row in rows[-limit:]:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        text = str(row.get("text") or row.get("content") or "").strip()
        if role in {"user", "assistant"} and text:
            clean.append({"role": role, "content": text if len(text) <= max_chars else text[: max_chars - 1] + "…"})
    return clean or None


def _waiting_note(repo: Path | None, *, can_read: bool) -> str:
    """What a question must know when a result waits for Keep or Discard ("" when none does).

    It is built from the request's own record, goes to the model with the question, and is not saved
    in the chat.
    """
    from local_coder.general.waiting import waiting_note

    return waiting_note(repo, can_read=can_read)


def _agent_chat(
    repo: Path | None,
    question: str,
    *,
    extra: str,
    listener: EventListener | None,
    cancel: threading.Event | None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A question answered by the agent with read-only tools across the home folder."""
    from local_coder.agent.runner import context_chars_for, hosted_tool_chat, run_ask
    from local_coder.agent.tools import inside_data_root
    from local_coder.general.chat_log import append_turn, recent_messages

    cfg = load_machine_config()

    def step(data: dict[str, Any]) -> None:
        if listener is not None:
            listener({"type": "activity", "data": data, "phrase": str(data.get("detail") or "")})

    asked = run_ask(
        f"{question}\n\n({extra})" if extra else question,
        project=repo,
        chat=hosted_tool_chat(cfg, cancel=cancel),
        context_chars=context_chars_for(cfg),
        history=recent_messages(repo, limit=10, max_chars=1500) if repo is not None else history,
        cancel=cancel,
        on_step=step,
    )
    opened = asked.opened if asked.opened is not None and asked.opened != repo else None
    if opened is not None and inside_data_root(opened):
        opened = None  # a waiting copy is never a project: the window would switch to it
    result: dict[str, Any] = {
        "kind": "chat",
        "summary": asked.answer,
        "hits": [],
        "model_commentary": asked.answer,
        "status": asked.status,
        "files_read": asked.read_paths,
        "open_project": str(opened) if opened is not None else None,
        "cost_usd": asked.cost_usd,
    }
    target = opened or repo
    if target is not None:
        append_turn(target, "user", question)
        result["turns"] = append_turn(target, "assistant", asked.answer)["turns"]
    if opened is not None:
        remember_project(opened)
    return result


def _start_preview_note(repo: Path) -> tuple[dict[str, Any] | None, str]:
    """Start preview as an action. The model writes the user-facing reply.

    The preview runs from the result that waits for Keep or Discard, or from a copy of the folder made now; the
    note says which. Nothing is written into the folder (`general/preview_copy.py`).
    """
    from local_coder.general.preview_copy import SOURCE_WAITING, preview_project

    preview = preview_project(repo)
    if preview.get("code") == "no_package":
        return None, "There is no package.json, so a web preview could not be started."
    url = str(preview.get("url") or "").strip()
    if preview.get("ok") and url:
        what = (
            "the change that is waiting for Keep or Discard (not in the folder yet)"
            if preview.get("source") == SOURCE_WAITING
            else "a copy of the project, made just now,"
        )
        return preview, f"A web preview of {what} is running at {url}."
    detail = str(preview.get("detail") or "The preview did not start.")
    return preview, f"The preview did not start: {detail}"


def _project_brief(repo: Path) -> str:
    """Compact facts the chat model (and shortcuts) can rely on."""
    from local_coder.general.inspect import inspect_project

    inspection = inspect_project(repo)
    lines = [
        f"Project name: {inspection.get('name')}",
        f"Project folder: {repo}",
        f"Languages: {', '.join(inspection.get('languages') or []) or 'unknown'}",
        f"Summary: {inspection.get('summary') or 'n/a'}",
    ]
    package = repo / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        deps = data.get("dependencies") if isinstance(data, dict) else {}
        lines.append(f"package.json name: {data.get('name') if isinstance(data, dict) else 'n/a'}")
        if isinstance(scripts, dict) and scripts:
            lines.append("npm scripts: " + ", ".join(sorted(scripts)))
        if isinstance(deps, dict) and deps:
            lines.append("dependencies: " + ", ".join(sorted(deps)[:12]))
        lines.append(
            "This is a Nicoder web project folder (typically React/Vite). "
            "It is not a separate Mac .app. The running UI is opened with the in-app preview."
        )
        missing = _missing_web_files(repo)
        if missing:
            lines.append("Missing web entry files: " + ", ".join(missing))
    return "\n".join(lines)


def _missing_web_files(repo: Path) -> list[str]:
    """Entry files a Vite starter needs before a preview can show UI."""
    needed = ("src/main.tsx", "src/App.tsx")
    return [name for name in needed if not (repo / name).is_file()]


def _chat_result(repo: Path, question: str, reply: str) -> dict[str, Any]:
    from local_coder.general.chat_log import append_turn

    append_turn(repo, "user", question)
    transcript = append_turn(repo, "assistant", reply)
    return {
        "kind": "chat",
        "summary": reply,
        "hits": [],
        "model_commentary": reply,
        "status": "answered",
        "turns": transcript["turns"],
    }


def load_project_chat(path: str | Path) -> dict[str, Any]:
    """Return the saved chat transcript for a project folder."""
    from local_coder.general.chat_log import load_chat

    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise AppError("That folder does not exist.")
    return load_chat(repo)


def clear_project_chat(path: str | Path) -> dict[str, Any]:
    """Clear the saved chat transcript for a project folder."""
    from local_coder.general.chat_log import clear_chat

    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise AppError("That folder does not exist.")
    return clear_chat(repo)


def _local_chat_completion(
    question: str,
    project_path: Path | None,
    *,
    extra: str = "",
    history: list[dict[str, str]] | None = None,
) -> str:
    """One short non-streaming chat completion from the owned local server or the hosted model."""
    ensure_server(wait_s=300)
    cfg = load_machine_config()
    brief = _project_brief(project_path) if project_path is not None else "No project folder is open yet."
    system = (
        "You are Nicoder, a local coding assistant inside a Mac app. "
        "You already have the project facts below — never claim you cannot see the "
        "project folder, the user's screen, or the code path. "
        "Use the recent conversation when the user refers to it. "
        "Do not claim you forgot earlier messages in this session. "
        "Answer like a person in a few sentences. Do not paste the fact list. "
        "Do not dump package.json, npm scripts, or dependency names unless asked. "
        "This project is edited inside Nicoder; the usual way to see the UI is the "
        "web preview, not Dock/Spotlight. "
        "Do not say the work must happen outside Nicoder. "
        "Do not claim you edited files.\n\n"
        f"{brief}"
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for row in history or []:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    user = question.strip()
    if extra.strip():
        user = f"{user}\n\n({extra.strip()})"
    messages.append({"role": "user", "content": user})
    payload = {
        "model": cfg.model_alias,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": min(512, int(cfg.max_tokens or 512)),
        "stream": False,
    }
    if is_hosted(cfg):
        try:
            data = hosted_chat(cfg, payload, thinking=False)
        except HostedError as exc:
            raise AppError(f"The model did not answer: {exc}", code="chat") from exc
    else:
        url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
        try:
            with httpx.Client(timeout=cfg.gen_timeout, trust_env=False, follow_redirects=False) as client:
                response = client.post(url, json=payload)
        except (httpx.HTTPError, OSError) as exc:
            raise AppError(f"The local model did not answer: {exc}", code="chat") from exc
        if response.status_code != 200:
            raise AppError(f"The local model returned HTTP {response.status_code}.", code="chat")
        try:
            data = response.json()
        except ValueError as exc:
            raise AppError("The local model returned an unreadable reply.", code="chat") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise AppError("The local model returned an empty reply.", code="chat")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "").strip()
    if not content and isinstance(choices[0], dict):
        content = str(choices[0].get("text") or "").strip()
    if not content:
        raise AppError("The local model returned an empty reply.", code="chat")
    return content


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
        cfg = apply_speed_defaults()
        return {"wrote": False, "path": str(path), "config": cfg.model_dump()}
    cfg = load_machine_config()
    written = write_machine_config(cfg)
    return {"wrote": True, "path": str(written), "config": cfg.model_dump()}


def apply_speed_defaults() -> Any:
    """Keep context modest and give the selected catalog model its generation budget."""
    cfg = load_machine_config()
    entry = None
    for row in list_models(cfg).get("models") or []:
        if row.get("selected"):
            entry = entry_by_id(str(row.get("id") or ""))
            break
    updates: dict[str, Any] = {}
    if entry is not None:
        if cfg.n_ctx > entry.n_ctx:
            updates["n_ctx"] = entry.n_ctx
    elif cfg.n_ctx >= 32768:
        alias = cfg.model_alias.lower()
        updates["n_ctx"] = 8192 if "4b" in alias else 16384
    if entry is not None:
        if cfg.gen_timeout < entry.gen_timeout:
            updates["gen_timeout"] = entry.gen_timeout
        if cfg.max_tokens < entry.max_tokens:
            updates["max_tokens"] = entry.max_tokens
    if not updates:
        return cfg
    updated = cfg.model_copy(update=updates)
    write_machine_config(updated)
    if "n_ctx" in updates:
        stop_owned_server()
    return updated


def select_model(model_id: str) -> dict[str, Any]:
    """Switch the machine config to a catalog model. Does not start a server."""
    apply_host_path()
    entry = entry_by_id(model_id)
    if entry is None:
        raise AppError(f"Unknown model: {model_id}", code="unknown_model")
    current = load_machine_config()
    if entry.provider != "local":
        if not find_api_key(entry.api_key_name)[0]:
            if entry.provider == "orcarouter":
                raise AppError(f"Connect OrcaRouter to use {entry.name}.", code="missing_key")
            raise AppError(
                f"{entry.name} needs the {entry.api_key_name} key, and it is not on this Mac.",
                code="missing_key",
            )
        placement: dict[str, Any] = {"model_file": "", "server_binary": "", "endpoint": entry.endpoint}
        loads = ""
    else:
        path = Path(entry.model_file).expanduser()
        if not path.is_file():
            raise AppError(f"{entry.name} is not installed on this Mac.", code="missing_model")
        if not (shutil.which(entry.server_binary) or Path(entry.server_binary).expanduser().is_file()):
            raise AppError(
                f"The server for {entry.name} was not found ({entry.server_binary}).",
                code="missing_binary",
            )
        placement = {"model_file": str(path.resolve()), "server_binary": entry.server_binary}
        if is_hosted(current):
            placement["endpoint"] = DEFAULT_ENDPOINT
        loads = " The model loads while you do that."
    stop_owned_server()
    cfg = current.model_copy(update={
        **placement,
        "model_alias": entry.model_alias,
        "n_ctx": entry.n_ctx,
        "quantization": entry.quantization,
        "max_tokens": entry.max_tokens,
        "gen_timeout": entry.gen_timeout,
        "provider": entry.provider,
        "api_key_name": entry.api_key_name,
        "temperature": entry.temperature,
        "top_p": entry.top_p,
        "reasoning": reasoning_pref(entry.id) if entry.reasoning_switch else "on",
        "reasoning_switch": entry.reasoning_switch,
    })
    write_machine_config(cfg)
    if entry.provider == "orcarouter":
        from . import orcarouter

        orcarouter.remember_model(entry.model_alias)
    return {
        "ok": True,
        "selected": entry.id,
        "config": cfg.model_dump(),
        "catalog": list_models(cfg),
        "detail": f"{entry.name} is selected. Open or create a project, then describe what you want.{loads}",
    }


def readiness() -> dict[str, Any]:
    """Prerequisites for ordinary app use. No model generation."""
    apply_host_path()
    cfg = load_machine_config()
    catalog = list_models(cfg)
    rows = []
    blocking = []
    for name, ok, detail in machine_status(cfg):
        rows.append({"name": name, "ok": ok, "detail": detail})
        if not ok and name != "endpoint":
            blocking.append(detail)
    status = server_status(cfg)
    if is_hosted(cfg):
        rows.append({
            "name": "model server",
            "ok": True,
            "detail": f"not needed: {cfg.model_alias} runs on {provider_place(cfg)}",
        })
    else:
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
    # Ordinary use is the general engine. Docker isolation is only required for
    # the legacy Python repair path, so a stopped Docker app is not a blocker.
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
        "build_id": BUILD_ID,
        "models": catalog["models"],
        "selected_model": catalog["selected"],
        "current_alias": catalog["current_alias"],
        "orcarouter": orca_status(),
        "reasoning": cfg.reasoning,
        "reasoning_switch": cfg.reasoning_switch,
    }


def _reasoning_prefs_path() -> Path:
    return config_dir() / "reasoning.json"


def reasoning_pref(model_id: str) -> str:
    """The reasoning mode last chosen for a model ("on" unless it was turned off)."""
    try:
        data = json.loads(_reasoning_prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "on"
    return "off" if isinstance(data, dict) and data.get(model_id) == "off" else "on"


def set_reasoning(mode: str) -> dict[str, Any]:
    """Turn the selected model's reasoning on or off; remembered for that model."""
    if mode not in {"on", "off"}:
        raise AppError("Reasoning is either on or off.", code="bad_request")
    cfg = load_machine_config()
    selected = list_models(cfg)["selected"]
    entry = entry_by_id(selected) if selected else None
    if not (cfg.reasoning_switch or (entry is not None and entry.reasoning_switch)):
        raise AppError("This model has no reasoning switch.", code="no_switch")
    path = _reasoning_prefs_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if selected:
        data[selected] = mode
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    write_machine_config(cfg.model_copy(update={"reasoning": mode, "reasoning_switch": True}))
    return {"ok": True, "reasoning": mode, "model": selected}


def orca_status() -> dict[str, Any]:
    """OrcaRouter connection for the app (never the key)."""
    from . import orcarouter

    return orcarouter.key_status()


def orca_save_key(value: str) -> dict[str, Any]:
    """Check and keep a pasted OrcaRouter key."""
    from . import orcarouter

    try:
        return orcarouter.save_pasted_key(value)
    except orcarouter.OrcaError as exc:
        raise AppError(str(exc), code="orcarouter") from exc
    except Exception:  # noqa: BLE001 — never let an unexpected error carry the key into a reply
        raise AppError("The key could not be saved.", code="orcarouter") from None


def orca_remove_key() -> dict[str, Any]:
    """Forget the OrcaRouter key on this Mac. A selected OrcaRouter model stays selected but unusable."""
    from . import orcarouter

    status = orcarouter.remove_key()
    status["detail"] = "OrcaRouter is disconnected on this Mac."
    return status


def orca_sign_in(listener: EventListener | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
    """Sign in with OrcaRouter in the browser and keep the new key."""
    from . import orcarouter

    def say(text: str) -> None:
        if listener is not None:
            listener({"type": "orca_sign_in", "phrase": text, "data": {"detail": text}})

    try:
        return orcarouter.sign_in(on_status=say, cancel=cancel)
    except orcarouter.OrcaError as exc:
        raise AppError(str(exc), code="orcarouter") from exc


def orca_models(*, refresh: bool = False) -> dict[str, Any]:
    """OrcaRouter's chat models with tool calls, plus recent and suggested picks."""
    from . import orcarouter

    try:
        catalog = orcarouter.load_catalog(refresh=refresh)
    except orcarouter.OrcaError as exc:
        raise AppError(str(exc), code="orcarouter") from exc
    cfg = load_machine_config()
    return {
        **catalog,
        "recent": orcarouter.recent_models(),
        "suggested": orcarouter.suggested(catalog["models"]),
        "selected": cfg.model_alias if cfg.provider == "orcarouter" else "",
        "connected": orcarouter.key_status()["connected"],
    }


def ensure_server(*, wait_s: int = 300) -> dict[str, Any]:
    """Start the owned local model if needed. Never adopt a foreign server. A hosted model needs none."""
    apply_host_path()
    cfg = load_machine_config()
    status = server_status(cfg)
    if is_hosted(cfg):
        return {"ok": True, "started": False, "hosted": True, "status": status}
    if status["healthy"] and status["owned"]:
        return {"ok": True, "started": False, "status": status}
    if status["healthy"] and not status["owned"]:
        raise AppError(
            f"A model server is already running at {cfg.endpoint} but Nicoder "
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
        from local_coder.general.coordinator import project_id_for, request_meta_path, review_payload

        if request_meta_path(project_id_for(Path(repo)), episode_id).exists():
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
        summary = "This request was discarded. Your project was not changed."
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
    if is_hosted(load_machine_config()) and not str(profile_name).startswith("fake:"):
        raise AppError(
            "The older Python engine runs only models on this Mac. Pick a local model in "
            "Settings, or switch the engine back to General.",
            code="unsupported",
        )
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
    from local_coder.general.coordinator import keep_request, project_id_for, request_meta_path

    folder = Path(repo)
    if request_meta_path(project_id_for(folder), episode_id).exists():
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
        from local_coder.general.coordinator import discard_request, project_id_for, request_meta_path

        if request_meta_path(project_id_for(Path(repo)), episode_id).exists():
            return discard_request(project_id_for(Path(repo)), episode_id)
    store = store or episode_store()
    return accept_mod.discard_episode(store, episode_id)


# What ends a run for the app: it shows the result (or the error) and starts the next waiting message.
_END_EVENTS = frozenset({"done", "stopped", "app_error", "handoff_written", "cancelled"})


class AppRuntime:
    """One helper process: at most one active editing episode."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        self.cancel_event = threading.Event()
        self.started_server = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self.active_episode: str | None = None
        self.active_repo: str | None = None
        self.last_error: str | None = None
        self._listeners: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        """Add a progress listener (RPC event stream)."""
        self._listeners.append(listener)

    def _emit(self, event: dict[str, Any]) -> None:
        raw_data = event.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        mapped = phrase_for_event(str(event.get("type", "")), data or {})
        existing = event.get("phrase")
        progress = existing if isinstance(existing, str) and existing.strip() else mapped
        payload = {**event, "phrase": progress}
        # Surface nested error detail for the Mac UI banner.
        if event.get("type") == "app_error" and "detail" not in payload and data.get("detail"):
            payload["detail"] = data["detail"]
        with self._emit_lock:
            for listener in list(self._listeners):
                listener(payload)

    def start_request(self, repo: str, request: str, profile_name: str = DEFAULT_PROFILE,
                      execution: str | None = None, engine: str | None = None,
                      kind: str = "create", generation_limit: int | None = None,
                      continue_from: str | None = None,
                      wall_seconds: int | None = None) -> dict[str, Any]:
        """Start a request on a worker thread and return immediately."""
        from local_coder.general.inspect import folder_problem

        problem = folder_problem(repo)
        if problem:
            raise AppError(problem["summary"], code=problem["verdict"])
        previous = self._thread
        if previous is not None and previous.is_alive() and previous is not threading.current_thread():
            # A run that just sent its end event is still closing for a moment (the app starts the next waiting
            # message right away).
            previous.join(timeout=3)
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
                    "generation_limit": generation_limit,
                    "continue_from": continue_from,
                    "wall_seconds": wall_seconds,
                },
                name="local-coder-episode",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return {"started": True, "episode_id": episode, "repo": self.active_repo,
                "engine": chosen}

    def _worker(self, **kwargs: Any) -> None:
        ended = False

        def emit(event: dict[str, Any]) -> None:
            nonlocal ended
            ended = ended or event.get("type") in _END_EVENTS
            self._emit(event)

        result: Any = None
        try:
            result = self._run(kwargs, emit)
        finally:
            if not ended:
                # Every run ends with an end event, or the app would show it working forever. A request the project
                # reads as a question is answered in its chat (`run_general_request` returns that answer).
                answered = isinstance(result, dict) and result.get("kind") in {"chat", "explain"}
                self._emit({"type": "done", "data": {"request_id": kwargs.get("episode_id"), "answered": answered},
                            "seq": None})
            with self._lock:
                self._thread = None

    def _run(self, kwargs: dict[str, Any], emit: EventListener) -> Any:
        from local_coder.general.coordinator import GeneralError

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
                return run_request(
                    kwargs["repo"], kwargs["request"],
                    profile_name=profile.removeprefix("legacy:"),
                    execution=kwargs.get("execution"),
                    state_dir=self.state_dir,
                    episode_id=kwargs["episode_id"],
                    listener=emit,
                    cancel_event=self.cancel_event,
                    ensure_local_server=False,
                )
            from local_coder.general.coordinator import run_general_request

            return run_general_request(
                kwargs["repo"], kwargs["request"],
                request_id=kwargs["episode_id"],
                kind=str(kwargs.get("kind") or "create"),
                listener=emit,
                cancel_event=self.cancel_event,
                generation_limit=kwargs.get("generation_limit"),
                continue_from=kwargs.get("continue_from"),
                wall_seconds=kwargs.get("wall_seconds"),
            )
        except (AppError, GeneralError) as exc:
            self.last_error = exc.detail
            emit({"type": "app_error", "data": {"detail": exc.detail, "code": exc.code}, "seq": None})
        except Exception as exc:  # noqa: BLE001 — surface unexpected helper failures
            # The app shows one plain sentence; the raw text and traceback go to stderr and logs/.
            record_error("request worker: unexpected error", exc)
            self.last_error = UNEXPECTED
            emit({"type": "app_error", "data": {"detail": UNEXPECTED, "code": "runtime"}, "seq": None})
        return None

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
