"""Blank Vite + React + TypeScript starter. No task-board implementation.

A request never writes it into a project folder: on an empty folder `lay_starter` puts it into the request's working
copy, so the review lists its files as the request's own new files and only Keep writes them. New Project and the
welcome chat make the folder and nothing in it: the folder has to exist (Keep never makes folders), and everything
that goes inside waits for Keep.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from local_coder.general.snapshot import iter_source_files

STARTER_NAME = "vite-react-ts"
FINDER_NOISE = ".DS_Store"
_SKIP_WORDS = {"a", "an", "the", "please", "can", "you", "i", "want", "to", "me"}
_SKIP_VERBS = {"build", "make", "create", "write", "implement", "add"}


def starter_root() -> Path:
    """Repository- or install-bundled blank starter."""
    here = Path(__file__).resolve()
    bundled = here.parents[1] / "data" / "starters" / STARTER_NAME
    if bundled.is_dir():
        return bundled
    repo = here.parents[3] / "starters" / STARTER_NAME
    return repo


def lay_starter(working_copy: Path) -> None:
    """Lay the blank starter's files into a request's working copy: the sticky `live/candidate`, never a project folder.

    Raises before any model call when the starter is missing from the install, so the model is never handed an empty
    working copy to fill from nothing. Only the files a snapshot sees are laid (no `node_modules`, no lock files).
    """
    source = starter_root()
    if not source.is_dir():
        raise FileNotFoundError(f"blank starter missing: {source}")
    for full in iter_source_files(source):
        target = working_copy / full.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(full, target)


def is_blank_folder(folder: Path) -> bool:
    """True when a folder holds nothing of his: it is empty, or holds only the `.DS_Store` Finder leaves in a folder.

    The one definition of "an empty folder" for the starter, the inspection and the project names. A Finder
    `.DS_Store` is not content: the folder looks empty to him, and counting it would skip the starter without a word.
    Every other hidden entry (`.git`) is his.
    """
    return all(entry.name == FINDER_NOISE and entry.is_file() for entry in Path(folder).iterdir())


def create_project(parent: str | Path, name: str) -> Path:
    """Create parent/name, empty: the folder has to exist, and the app reaches it only when a Keep writes it.

    The starter is part of the first request's working copy (`lay_starter`), so this puts nothing in the folder.
    """
    cleaned = name.strip().replace("/", "-").replace("\\", "-")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Choose a project name.")
    dest = Path(parent).expanduser().resolve() / cleaned
    if dest.exists() and dest.is_dir() and not is_blank_folder(dest):
        raise ValueError(f"{dest} already exists and is not empty.")
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def projects_home() -> Path:
    """Default folder for projects created from the welcome chat."""
    override = os.environ.get("LOCAL_CODER_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Local Coder Projects"


def project_name_from_request(text: str) -> str:
    """Turn the first request into a short folder name."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]+", " ", text.strip().lower())
    words = [part for part in cleaned.replace("_", " ").split() if part]
    if words and words[0] in _SKIP_VERBS and len(words) > 1:
        words = words[1:]
    words = [word for word in words if word not in _SKIP_WORDS][:6]
    slug = "-".join(words)[:40].strip("-")
    return slug or "new-project"


def allocate_project_name(parent: str | Path, name: str) -> str:
    """Return name, or name-2 if that folder is already used."""
    root = Path(parent).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / name
    if not dest.exists() or (dest.is_dir() and is_blank_folder(dest)):
        return name
    for index in range(2, 100):
        candidate = f"{name}-{index}"
        path = root / candidate
        if not path.exists():
            return candidate
    raise ValueError(f"could not allocate a folder name under {root}")
