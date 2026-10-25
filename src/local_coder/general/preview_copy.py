"""The web preview a question asks for runs from a copy, never in the project's own folder.

`start_preview` runs `npm install` and `npm run dev` in the folder it is given. Given the project's own folder that
leaves `node_modules` and a rewritten `package-lock.json` in it, and a dev server running there, before anything was
kept (invariant 5). So a question that asks for the preview ("open the app") is shown one of two things:

- the result that waits for Keep or Discard, from that request's own `candidate/`: the same code the Changes list
  beside the Preview tab shows (`"source": "waiting"`); or
- a copy of the folder made now, at `<data>/projects/<id>/preview/` (`"source": "copy"`).

The copy is not a request: it has no record, no History row and no place in the stack, and only the dev server reads
it. It holds the folder's source files as they are when the question is asked (`sync_source_tree`: no `node_modules`,
lock files, `.env` or links) and its own `node_modules`: the one it installed earlier, or a copy-on-write clone of
the folder's when the folder has them for the same `package.json`, so the preview starts as fast as it did in place.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from local_coder.general import packages
from local_coder.general import preview as preview_module
from local_coder.general.coordinator import project_dir, project_id_for
from local_coder.general.inspect import folder_problem
from local_coder.general.snapshot import sync_source_tree
from local_coder.general.waiting import find_waiting_result

COPY_FOLDER = "preview"
SOURCE_WAITING = "waiting"
SOURCE_COPY = "copy"


def preview_copy_dir(project_id: str) -> Path:
    """Where a project's preview copy lives. Creates nothing."""
    return project_dir(project_id) / COPY_FOLDER


def preview_project(folder: str | Path) -> dict[str, Any]:
    """Start the preview a question asked for and answer like `start_preview`, plus `source` ("waiting" or "copy").

    The folder is only read: a result that waits is previewed from its own `candidate/`, else a copy of the folder
    is brought up to date and previewed. A refusal is `{"ok": False, "code", "detail"}` with one plain sentence and
    leaves nothing behind: a path that is gone or is a file, one inside Local Coder's data folder (its working
    copies are not projects), and a folder with no `package.json` (and no waiting result that has one).
    """
    text = str(folder).strip()
    if not text:
        return _refusal("missing", "Choose a project folder to preview.")
    path = Path(text).expanduser()
    problem = folder_problem(path)
    if problem is not None:
        return _refusal(problem["verdict"], problem["summary"])
    if _inside_data_root(path):
        return _refusal(
            "inside_data_root",
            "That is inside Local Coder's own data folder, not a project, so no preview was started.",
        )
    project_id = project_id_for(path)
    waiting = _waiting_candidate(path)
    if waiting is not None:
        _clone_modules(path, waiting)
        return {**preview_module.start_preview(waiting, project_id), "source": SOURCE_WAITING}
    if not (path / "package.json").is_file():
        return _refusal("no_package", "This project has no package.json yet.")
    copy = preview_copy_dir(project_id)
    try:
        _refresh_copy(path, copy)
    except OSError as exc:
        return _refusal("copy_failed", f"The preview copy could not be made: {exc.strerror or exc}.")
    return {**preview_module.start_preview(copy, project_id), "source": SOURCE_COPY}


def _refusal(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "detail": detail}


def _inside_data_root(path: Path) -> bool:
    from local_coder.agent.tools import inside_data_root

    return inside_data_root(path)


def _waiting_candidate(folder: Path) -> Path | None:
    """The `candidate/` of the result that waits for Keep or Discard, when it can be previewed."""
    try:
        waiting = find_waiting_result(folder)
    except Exception:  # noqa: BLE001 — a bad record must never fail a question
        return None
    if waiting is None or not (waiting.candidate / "package.json").is_file():
        return None
    return waiting.candidate


def _refresh_copy(folder: Path, copy: Path) -> None:
    """Bring the copy's source files up to date with the folder.

    A file whose bytes the copy already holds is not written again: the dev server watches the copy, and a rewritten
    `vite.config.ts` restarts it. The copy's `node_modules` stay, because `start_preview` installs only when they
    are missing, unless `package.json` changed since the copy was made: then the running preview stops (a server
    that stays up would never see the packages installed again) and the packages go, before anything is written, so
    a run that dies half way cannot leave packages that no longer match.
    """
    if _bytes(folder / "package.json") != _bytes(copy / "package.json") and os.path.lexists(copy / "node_modules"):
        _stop_preview_in(copy)
        _remove(copy / "node_modules")
    sync_source_tree(folder, copy, skip_same=True)
    _clone_modules(folder, copy)


def _stop_preview_in(folder: Path) -> None:
    """Stop the preview when it is the one running in `folder`; any other is stopped by `start_preview` itself."""
    record = preview_module.read_preview()
    if record and record.get("dir") == str(folder):
        preview_module.stop_preview()


def _clone_modules(folder: Path, target: Path) -> None:
    """Give `target` a copy-on-write clone of the folder's `node_modules`, so its preview needs no `npm install`.

    Only when `target` has none, the folder's are a real folder (not a link: the dev server and Vite's cache write
    into `target`'s own) and `target` has the folder's `package.json`, byte for byte (packages installed for another
    one would be wrong). The clone is made beside `target` and moved in only when whole and when no link in it leads
    out of `target` (`packages.clone_modules`), so a clone that fails or is cut short never leaves half a
    `node_modules` that `start_preview` would trust: it installs in `target` instead. The folder's `node_modules`
    are only read.
    """
    package = _bytes(target / "package.json")
    if package is None or package != _bytes(folder / "package.json"):
        return
    packages.clone_modules(folder / packages.MODULES, target)


def _remove(path: Path) -> None:
    """Delete a file, link or folder; nothing when it is not there. A folder that cannot go raises OSError."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None
