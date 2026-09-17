"""Project detection and request classification for the general engine."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from local_coder.controller import git_is_dirty, git_revision


def _count_by_suffix(root: Path, suffixes: tuple[str, ...], limit: int = 400) -> int:
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
               for part in path.parts):
            continue
        if path.suffix.lower() in suffixes or path.name.endswith(suffixes):
            count += 1
            if count >= limit:
                return count
    return count

_EXPLAIN_HINT = (
    "explain", "what does", "what is", "how does", "how do", "describe",
    "show me", "walk me through", "summarize",
)
_MOBILE_HINT = ("swiftui", "uikit", "xcode project", "android studio", "kotlin android")


def inspect_project(path: str | Path) -> dict[str, Any]:
    """Describe what this folder is and what is missing. Does not refuse JS or dirty trees."""
    repo = Path(path).expanduser().resolve()
    report: dict[str, Any] = {
        "path": str(repo),
        "name": repo.name,
        "exists": repo.is_dir(),
        "supported": False,
        "verdict": "missing",
        "summary": "",
        "issues": [],
        "notes": [],
        "python_files": 0,
        "test_files": 0,
        "web_files": 0,
        "has_pytest": False,
        "has_config": False,
        "has_package_json": False,
        "has_node_modules": False,
        "scripts": {},
        "execution": None,
        "engine": "general",
        "git": {"present": False, "dirty": False, "revision": None},
        "languages": [],
        "can_create": False,
        "can_preview": False,
        "exclusions": ["node_modules", "dist", "build", ".git", ".venv", ".env", "secrets"],
    }
    if not repo.exists():
        report["can_create"] = True
        report["summary"] = "This folder does not exist yet. You can create a new project here."
        report["issues"].append({"code": "missing", "message": report["summary"], "blocking": False})
        return report
    if not repo.is_dir():
        report["summary"] = "That path is a file, not a folder."
        report["issues"].append({"code": "not_a_folder", "message": report["summary"], "blocking": True})
        return report

    python_files = _count_by_suffix(repo, (".py",))
    web_files = _count_by_suffix(repo, (".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".vue"))
    test_files = 0
    if (repo / "tests").is_dir():
        test_files += len(list((repo / "tests").glob("test_*.py")))
    test_files += _count_by_suffix(repo, (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
    report["python_files"] = python_files
    report["web_files"] = web_files
    report["test_files"] = test_files
    languages: list[str] = []
    if python_files:
        languages.append("Python")
    if web_files:
        languages.append("JavaScript/TypeScript")
    report["languages"] = languages

    package = repo / "package.json"
    report["has_package_json"] = package.exists()
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        if isinstance(scripts, dict):
            report["scripts"] = {str(k): str(v) for k, v in scripts.items()}
            report["can_preview"] = any(key in scripts for key in ("dev", "start", "preview"))
    report["has_node_modules"] = (repo / "node_modules").is_dir()
    report["has_pytest"] = (
        (repo / "pytest.ini").exists()
        or (repo / "conftest.py").exists()
        or test_files > 0
    )
    from local_coder.config import CONFIG_FILENAME

    report["has_config"] = (repo / CONFIG_FILENAME).exists()
    if (repo / ".git").exists():
        report["git"] = {
            "present": True,
            "dirty": git_is_dirty(repo),
            "revision": git_revision(repo)[:12],
        }
        if report["git"]["dirty"]:
            report["notes"].append(
                "This folder has uncommitted work. Local Coder will copy the current files, "
                "including those edits, and will not require a commit."
            )
    else:
        report["notes"].append("This folder is not a git checkout. That is fine.")

    empty = not any(repo.iterdir()) if repo.is_dir() else True
    if empty and not languages:
        report["supported"] = True
        report["verdict"] = "empty"
        report["can_create"] = True
        report["summary"] = (
            "This folder is empty. Describe the application you want, and Local Coder "
            "will try to build it here."
        )
        return report

    native = _looks_native(repo)
    if native:
        report["verdict"] = "limited"
        report["supported"] = True
        report["summary"] = (
            "This looks like a native or mobile project. This version is verified for "
            "React/TypeScript web apps. You can still ask, but the result may be incomplete."
        )
        report["issues"].append({"code": "native", "message": report["summary"], "blocking": False})
        return report

    report["supported"] = True
    report["verdict"] = "ready"
    if report["has_package_json"] and "JavaScript/TypeScript" in languages:
        missing = []
        if not report["has_node_modules"]:
            missing.append("project packages are not installed yet")
        extra = f" {'; '.join(missing)}." if missing else ""
        report["summary"] = (
            "This looks like a JavaScript/TypeScript project. You can describe a change "
            "in ordinary language. Original files stay untouched until you keep a result."
            + extra
        )
    elif python_files:
        report["summary"] = (
            "This is a Python project. You can describe a change in ordinary language. "
            "Original files stay untouched until you keep a result."
        )
    else:
        report["summary"] = (
            "You can describe what you want built or changed. Original files stay "
            "untouched until you keep a result."
        )
    return report


def classify_request(text: str, inspection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify a user request. Web and new-app work is allowed."""
    request = text.strip()
    if not request:
        return {"kind": "empty", "summary": "Write what you would like to build or change.",
                "detail": "The request box is empty."}
    lowered = request.lower()
    if any(lowered.startswith(prefix) for prefix in _EXPLAIN_HINT):
        return {"kind": "explain", "summary": "This will look up the selected code and stay read-only.",
                "detail": "No files will be edited."}
    if any(hint in lowered for hint in _MOBILE_HINT):
        return {
            "kind": "code",
            "summary": "This version is verified for React/TypeScript web apps, not native mobile toolchains.",
            "detail": "The request will still be attempted, with that limit shown.",
            "limited": True,
        }
    if inspection and not inspection.get("exists"):
        return {"kind": "code", "summary": "This will create a new project from your description.",
                "detail": "A blank starter can be prepared; the local model writes the application."}
    if inspection and inspection.get("verdict") == "missing" and not inspection.get("can_create"):
        return {"kind": "empty", "summary": inspection.get("summary") or "That folder is not available.",
                "detail": inspection.get("verdict")}
    return {"kind": "code", "summary": "This will try a bounded local coding attempt.",
            "detail": "You will review any proposed change before it is kept."}


def _looks_native(repo: Path) -> bool:
    names = {path.name.lower() for path in repo.iterdir()} if repo.is_dir() else set()
    return bool(names & {"xcodeproj", "pbxproj"}) or any(
        name.endswith(".xcodeproj") or name.endswith(".xcworkspace") for name in names
    )
