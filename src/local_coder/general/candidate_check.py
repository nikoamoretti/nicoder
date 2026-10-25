"""Runtime-managed checks on a disposable candidate. No host shell for the model."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

_FROM_IMPORT = re.compile(r"""(?:from|import)\s+['"](\.[^'"]+)['"]""")
_STARTER_MARKERS = ("empty on purpose", "<h1>new app</h1>")
_WEB_SUFFIX = {".tsx", ".ts", ".jsx", ".js"}
_MODULE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".css")


def unresolved_imports(root: Path, rel: str, text: str) -> list[str]:
    """Return problems for relative imports that do not exist on disk."""
    problems: list[str] = []
    base = (root / rel).parent
    for spec in _FROM_IMPORT.findall(text or ""):
        if not _module_exists(root, base, spec):
            problems.append(f"{rel}: cannot resolve import {spec!r}")
    return problems


def empty_app_reason(root: Path) -> str | None:
    """Why this tree is still a blank starter, or None."""
    for rel in ("src/App.tsx", "src/App.jsx"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lower = text.lower()
        if any(marker in lower for marker in _STARTER_MARKERS):
            return "the preview is still the empty starter page"
        return None
    if not (root / "src").is_dir():
        return "the preview has no application entry"
    return None


def scan_candidate_sources(root: Path) -> list[str]:
    """Scan web sources for syntax issues and missing relative imports."""
    from local_coder.general.source_check import scan_text

    problems: list[str] = []
    src = root / "src"
    if not src.is_dir():
        return problems
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _WEB_SUFFIX:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        problems.extend(scan_text(rel, text))
        problems.extend(unresolved_imports(root, rel, text))
    return problems


def run_project_build(root: Path, *, timeout: int = 180) -> dict[str, Any]:
    """Run `npm run build` in the candidate when the project already has node_modules."""
    package = root / "package.json"
    if not package.is_file():
        return {"ran": False, "ok": True, "code": "not_run", "detail": "no package.json"}
    if not (root / "node_modules").is_dir():
        return {"ran": False, "ok": True, "code": "not_run", "detail": "node_modules is not installed yet"}
    npm = _which("npm")
    if npm is None:
        return {"ran": False, "ok": False, "code": "npm_missing", "detail": "npm was not found on this Mac."}
    try:
        proc = subprocess.run(
            [npm, "run", "build"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "code": "build_timeout", "detail": f"npm run build exceeded {timeout}s."}
    detail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-8000:]
    if proc.returncode != 0:
        return {"ran": True, "ok": False, "code": "build_failed", "detail": detail.strip() or "npm run build failed"}
    return {"ran": True, "ok": True, "code": "ok", "detail": "npm run build passed"}


def check_candidate(root: Path) -> dict[str, Any]:
    """Run source and optional project-build checks. Never a blanket success."""
    source_problems = scan_candidate_sources(root)
    empty = empty_app_reason(root)
    build = run_project_build(root)
    if build.get("ran") and build.get("ok"):
        # The project's own build compiled it, so the quick scan's guesses get no veto: on 466 files of Nico's
        # working projects it still raised 21 false alarms (2026-10-25). It only judges when no build can run.
        source_problems = []
    ok = not source_problems and build.get("ok") is True and empty is None
    detail_parts = list(source_problems)
    if empty:
        detail_parts.append(empty)
    if not build.get("ok"):
        detail_parts.append(str(build.get("detail") or "build failed"))
    return {
        "ok": ok,
        "source_ok": not source_problems,
        "source_problems": source_problems[:20],
        "empty_page": empty,
        "build": build,
        "detail": "\n".join(detail_parts)[:8000],
    }


def check_retry_message(verification: dict[str, Any]) -> str:
    """Ask the model to fix the latest real check error. Does not prescribe a patch."""
    detail = str(verification.get("detail") or "checking failed").strip()
    return (
        "The files you wrote failed checking, so this is not a finished application. "
        "Keep the original user request. Do not weaken it. "
        "The latest useful error from the candidate is:\n"
        f"{detail[:1800]}\n"
        "Reply again with write or edit blocks for the candidate. "
        "Do not dump every file."
    )


def write_verification(request_root: Path, payload: dict[str, Any]) -> None:
    """Persist check evidence next to the request, outside the model prompt."""
    path = request_root / "verification.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _module_exists(root: Path, base: Path, spec: str) -> bool:
    dest = (base / spec)
    try:
        dest.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if dest.is_file():
        return True
    if dest.is_dir():
        return any((dest / f"index{suffix}").is_file() for suffix in _MODULE_SUFFIXES)
    return any(Path(str(dest) + suffix).is_file() for suffix in _MODULE_SUFFIXES)


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)
