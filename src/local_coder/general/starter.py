"""Blank Vite + React + TypeScript starter. No task-board implementation."""
from __future__ import annotations

import shutil
from pathlib import Path

STARTER_NAME = "vite-react-ts"


def starter_root() -> Path:
    """Repository- or install-bundled blank starter."""
    here = Path(__file__).resolve()
    bundled = here.parents[1] / "data" / "starters" / STARTER_NAME
    if bundled.is_dir():
        return bundled
    repo = here.parents[3] / "starters" / STARTER_NAME
    return repo


def copy_starter(dest: Path) -> Path:
    """Copy the blank starter into an empty or new folder."""
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise ValueError(f"refusing to copy a starter into a non-empty folder: {dest}")
    source = starter_root()
    if not source.is_dir():
        raise FileNotFoundError(f"blank starter missing: {source}")
    shutil.copytree(source, dest, dirs_exist_ok=True)
    return dest


def create_project(parent: str | Path, name: str) -> Path:
    """Create parent/name and copy the blank starter into it."""
    cleaned = name.strip().replace("/", "-").replace("\\", "-")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Choose a project name.")
    dest = Path(parent).expanduser().resolve() / cleaned
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        raise ValueError(f"{dest} already exists and is not empty.")
    dest.mkdir(parents=True, exist_ok=True)
    return copy_starter(dest)
