"""Delegation for other agents (Claude Code through MCP): cheap-model reading and drafted changes.

Always uses the hosted Nemotron 3.5 Super 120B catalog entry, whatever model the
Nicoder app has selected. `explore` is read-only and stays inside the given
project. `create_draft` changes a private copy of the project; nothing reaches
the project until `apply_draft` (which refuses if the same files changed
meanwhile), and `discard_draft` throws the copy away.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from local_coder.catalog import entry_by_id
from local_coder.general.snapshot import EXCLUDE, apply_delta, compute_delta, copy_snapshot, index_tree, unified_diff
from local_coder.machine import MachineConfig

from .access import AccessPolicy
from .loop import AgentSession, ChatFn
from .prompts import delegate_draft_prompt, delegate_explore_prompt
from .runner import context_chars_for, hosted_tool_chat, runs_dir
from .tools import ToolBox, looks_like_project

DELEGATE_MODEL_ID = "nemotron35-super"
EXPLORE_STEPS = 30
EXPLORE_SECONDS = 420
EXPLORE_CONTEXT_CHARS = 100_000
DRAFT_STEPS = 60
DRAFT_SECONDS = 900
DIFF_CHARS = 15_000
MAX_PROJECT_FILES = 20_000
MAX_PROJECT_BYTES = 1_000_000_000
_DRAFT_ID = re.compile(r"^d\d{8}-\d{6}-[0-9a-f]{4}$")


class DelegateError(Exception):
    """A delegated task was refused before any model call. The message is for the caller."""


def delegate_config() -> MachineConfig:
    """The hosted model every delegated task uses."""
    entry = entry_by_id(DELEGATE_MODEL_ID)
    if entry is None:
        raise DelegateError(f"The model catalog has no {DELEGATE_MODEL_ID} entry.")
    return MachineConfig(
        model_alias=entry.model_alias,
        endpoint=entry.endpoint,
        provider=entry.provider,
        api_key_name=entry.api_key_name,
        n_ctx=entry.n_ctx,
        max_tokens=entry.max_tokens,
        gen_timeout=entry.gen_timeout,
        quantization=entry.quantization,
    )


def drafts_dir() -> Path:
    """Where draft copies live until they are applied or discarded."""
    from local_coder.engines.opencode_config import data_root

    return data_root() / "drafts"


def _project(raw: str | Path | None, home: Path, *, required: bool) -> Path | None:
    """A real project folder inside the home folder, or None when none was given."""
    if raw is None or not str(raw).strip():
        if required:
            raise DelegateError("project_path is required.")
        return None
    path = Path(os.path.realpath(Path(str(raw)).expanduser()))
    home_real = Path(os.path.realpath(home))
    if not path.is_dir():
        raise DelegateError(f"{path} is not a folder.")
    if path == home_real or home_real not in path.parents:
        raise DelegateError(f"{path} must be a project folder inside {home_real}.")
    reason = AccessPolicy(home=home_real).denied_reason(path)
    if reason:
        raise DelegateError(reason)
    return path


def _measure(project: Path) -> tuple[int, int]:
    """Files and bytes a snapshot copy would take (dependency and build folders excluded)."""
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDE]
        for name in filenames:
            files += 1
            try:
                size += (Path(dirpath) / name).lstat().st_size
            except OSError:
                continue
            if files > MAX_PROJECT_FILES or size > MAX_PROJECT_BYTES:
                return files, size
    return files, size


def explore(
    question: str,
    *,
    project: str | Path | None = None,
    chat: ChatFn | None = None,
    on_activity: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Answer a question about code with read-only tools; returns the answer and what was read."""
    home = home or Path.home()
    if not question.strip():
        raise DelegateError("The question is empty.")
    folder = _project(project, home, required=False)
    cfg = delegate_config()
    run_root = runs_dir() / f"delegate-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    toolbox = ToolBox(
        AccessPolicy(home=home, project=folder, limit_to=folder),
        cwd=folder or home,
        run_tmp=run_root / "tmp",
        cancel=cancel,
        allow_find_project=folder is None,
    )
    session = AgentSession(
        chat=chat or hosted_tool_chat(cfg),
        toolbox=toolbox,
        system=delegate_explore_prompt(home=home, project=folder),
        context_chars=min(context_chars_for(cfg), EXPLORE_CONTEXT_CHARS),
        on_activity=on_activity,
        cancel=cancel,
    )
    started = time.monotonic()
    run = session.ask(question, max_steps=EXPLORE_STEPS, deadline=started + EXPLORE_SECONDS, hurry_after=0.5)
    seconds = round(time.monotonic() - started, 1)
    transcript = session.save(
        run_root / "transcript.json", mode="delegate-explore", status=run.status, seconds=seconds,
        project=str(folder) if folder else None,
    )
    shutil.rmtree(run_root / "tmp", ignore_errors=True)
    return {
        "answer": run.answer,
        "status": run.status,
        "files_read": list(toolbox.read_paths),
        "model_calls": run.steps,
        "seconds": seconds,
        "transcript": str(transcript),
    }


def create_draft(
    project: str | Path,
    task: str,
    *,
    chat: ChatFn | None = None,
    on_activity: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Let the model make a change in a private copy of the project. Nothing touches the project."""
    home = home or Path.home()
    if not task.strip():
        raise DelegateError("The task is empty.")
    folder = _project(project, home, required=True)
    assert folder is not None
    if not looks_like_project(folder):
        raise DelegateError(f"{folder} does not look like a project (no .git, package.json, pyproject.toml, …).")
    files, size = _measure(folder)
    if files > MAX_PROJECT_FILES or size > MAX_PROJECT_BYTES:
        raise DelegateError(f"{folder} is too large to copy for a draft ({files}+ files, {size // 1_000_000} MB).")
    draft_id = f"d{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    root = drafts_dir() / draft_id
    baseline, work = root / "baseline", root / "work"
    copy_snapshot(folder, baseline)
    copy_snapshot(baseline, work)
    if (folder / "node_modules").is_dir():
        # APFS clone: instant and space-free, so builds and tests can run in the copy.
        subprocess.run(["/bin/cp", "-cR", str(folder / "node_modules"), str(work / "node_modules")],
                       capture_output=True, timeout=120, check=False)
    cfg = delegate_config()
    toolbox = ToolBox(
        AccessPolicy(home=home, workdir=work, project=folder, limit_to=folder),
        cwd=work,
        run_tmp=root / "tmp",
        cancel=cancel,
        allow_find_project=False,
    )
    session = AgentSession(
        chat=chat or hosted_tool_chat(cfg),
        toolbox=toolbox,
        system=delegate_draft_prompt(project=folder, workdir=work),
        context_chars=context_chars_for(cfg),
        on_activity=on_activity,
        cancel=cancel,
    )
    started = time.monotonic()
    run = session.ask(task, max_steps=DRAFT_STEPS, deadline=started + DRAFT_SECONDS, hurry_after=0.7)
    seconds = round(time.monotonic() - started, 1)
    changed = sorted(compute_delta(index_tree(baseline), index_tree(work)).touched())
    diff = unified_diff(baseline, work)
    meta = {
        "draft_id": draft_id,
        "project": str(folder),
        "task": task,
        "status": run.status,
        "summary": run.answer,
        "files": changed,
        "commands": toolbox.commands,
        "model_calls": run.steps,
        "seconds": seconds,
        "created": time.time(),
        "state": "open" if changed else "empty",
    }
    (root / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    session.save(root / "transcript.json", mode="delegate-draft", status=run.status, seconds=seconds)
    shutil.rmtree(root / "tmp", ignore_errors=True)
    shown = diff if len(diff) <= DIFF_CHARS else diff[:DIFF_CHARS] + "\n… (diff cut; read the files under work_folder)"
    return {**meta, "diff": shown, "work_folder": str(work)}


def _draft_root(draft_id: str) -> Path:
    if not _DRAFT_ID.match(draft_id or ""):
        raise DelegateError(f"{draft_id!r} is not a draft id.")
    root = drafts_dir() / draft_id
    if not (root / "meta.json").is_file():
        raise DelegateError(f"Draft {draft_id} does not exist (it may have been applied or discarded).")
    return root


def apply_draft(draft_id: str) -> dict[str, Any]:
    """Copy the draft's changes into the project. Refuses if those files changed since the draft began."""
    root = _draft_root(draft_id)
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    if meta.get("state") != "open":
        raise DelegateError(f"Draft {draft_id} is {meta.get('state')}; there is nothing to apply.")
    result = apply_delta(Path(meta["project"]), root / "baseline", root / "work")
    if result.get("ok"):
        shutil.rmtree(root, ignore_errors=True)
        return {"applied": True, "project": meta["project"], "files": result.get("applied") or []}
    details = {key: value for key, value in result.items() if key not in {"ok", "applied"}}
    return {"applied": False, "project": meta["project"], **details}


def discard_draft(draft_id: str) -> dict[str, Any]:
    """Throw a draft away. The project was never touched."""
    root = _draft_root(draft_id)
    shutil.rmtree(root, ignore_errors=True)
    return {"discarded": True, "draft_id": draft_id}
