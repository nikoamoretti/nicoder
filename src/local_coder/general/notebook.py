"""Compact project notebook. Facts come from execution; hypotheses are labeled."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def notebook_path(work_dir: Path) -> Path:
    return work_dir / "notebook.json"


def load_notebook(work_dir: Path) -> dict[str, Any]:
    path = notebook_path(work_dir)
    if not path.exists():
        return {
            "goals": [],
            "decisions": [],
            "files_changed": [],
            "failing_checks": [],
            "limitations": [],
            "next_action": "",
            "hypotheses": [],
        }
    empty = {
        "goals": [],
        "decisions": [],
        "files_changed": [],
        "failing_checks": [],
        "limitations": [],
        "next_action": "",
        "hypotheses": [],
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return data


def save_notebook(work_dir: Path, notebook: dict[str, Any]) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    path = notebook_path(work_dir)
    path.write_text(json.dumps(notebook, indent=2) + "\n", encoding="utf-8")


def update_notebook(
    work_dir: Path,
    *,
    goal: str | None = None,
    files_changed: list[str] | None = None,
    failing_checks: list[str] | None = None,
    next_action: str | None = None,
    limitation: str | None = None,
    hypothesis: str | None = None,
) -> dict[str, Any]:
    notebook = load_notebook(work_dir)
    if goal and goal not in notebook.get("goals", []):
        notebook.setdefault("goals", []).append(goal)
    if files_changed is not None:
        notebook["files_changed"] = list(files_changed)
    if failing_checks is not None:
        notebook["failing_checks"] = list(failing_checks)
    if next_action is not None:
        notebook["next_action"] = next_action
    if limitation:
        notebook.setdefault("limitations", []).append(limitation)
    if hypothesis:
        notebook.setdefault("hypotheses", []).append({"text": hypothesis, "kind": "hypothesis"})
    save_notebook(work_dir, notebook)
    return notebook
