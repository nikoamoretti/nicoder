"""Ask the local model for file bodies and apply them onto a snapshot.

Local Coder owns apply. The model cannot write the original tree. Keep/Discard
still decide what lands in the user's folder.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from local_coder.general.snapshot import EXCLUDE
from local_coder.general.source_check import scan_text
from local_coder.machine import load_machine_config
from local_coder.server import ServerError, server_start

_FENCE = re.compile(r"```(?P<header>[^\n]*)\n(?P<body>.*?)```", re.S)
_THINK = re.compile(r"<think>.*?</think>", re.S)
_ALLOWED_SUFFIX = {".tsx", ".ts", ".css", ".jsx", ".js", ".html", ".cjs", ".mjs", ".json"}
_ALLOWED_ROOTS = {"src", "electron", "public"}
_ROOT_NAMES = {
    "index.html",
    "package.json",
    "vite.config.ts",
    "vite.config.js",
    "electron-builder.json",
    "electron-builder.yml",
}


def is_blank_starter(root: Path) -> bool:
    """True when this tree is still the empty Vite starter."""
    if not (root / "package.json").is_file():
        return False
    app = root / "src" / "App.tsx"
    if not app.is_file():
        return True
    try:
        text = app.read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "empty on purpose" in text or "<h1>new app</h1>" in text


def safe_rel(rel: str) -> str | None:
    """Return a project-relative path, or None if it is unsafe."""
    cleaned = rel.strip().strip("`").replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        return None
    path = Path(cleaned)
    if path.is_absolute() or ".." in path.parts:
        return None
    if any(part in EXCLUDE or part.endswith(".egg-info") for part in path.parts):
        return None
    suffix = path.suffix.lower()
    if suffix not in _ALLOWED_SUFFIX and path.name not in _ROOT_NAMES:
        return None
    posix = path.as_posix()
    if len(path.parts) == 1:
        if posix in _ROOT_NAMES or suffix in {".html", ".css", ".ts", ".js", ".cjs", ".mjs", ".tsx", ".jsx"}:
            return posix
        return None
    if path.parts[0] not in _ALLOWED_ROOTS:
        return None
    if suffix == ".json" and path.parts[0] != "electron":
        return None
    return posix


def _path_from_header(header: str) -> str | None:
    text = header.strip()
    if not text:
        return None
    if ":" in text and "/" in text.split(":", 1)[-1]:
        text = text.split(":", 1)[-1].strip()
    for token in reversed(text.split()):
        candidate = safe_rel(token)
        if candidate:
            return candidate
    return safe_rel(text)


def parse_file_blocks(text: str) -> dict[str, str]:
    """Extract path → body from fenced blocks. Last block for a path wins."""
    cleaned = _THINK.sub("", text or "")
    files: dict[str, str] = {}
    for match in _FENCE.finditer(cleaned):
        rel = _path_from_header(match.group("header") or "")
        if not rel:
            continue
        body = match.group("body")
        if not body.endswith("\n"):
            body += "\n"
        files[rel] = body
    return files


def reply_prose(text: str) -> str:
    """Plain-language text before the first file fence."""
    cleaned = _THINK.sub("", text or "").strip()
    before = cleaned.split("```", 1)[0].strip()
    if len(before) < 24:
        return ""
    return before[:1200]


def fallback_explanation(written: list[str]) -> str:
    """Honest fallback when the model wrote files but no prose."""
    if not written:
        return ""
    if len(written) == 1:
        return (
            f"I updated {written[0]}. Your original folder is unchanged until you Keep."
        )
    return (
        f"I updated {len(written)} files ({', '.join(written)}). "
        "Your original folder is unchanged until you Keep."
    )


def checked_blocks(raw: str) -> tuple[dict[str, str], list[str]]:
    """Parse fenced files and list problems Vite cannot compile."""
    files = parse_file_blocks(raw)
    problems: list[str] = []
    for rel, body in files.items():
        problems.extend(scan_text(rel, body))
    return files, problems


def apply_files(root: Path, files: dict[str, str]) -> list[str]:
    """Write parsed files under root. Returns relative paths that were written."""
    written: list[str] = []
    for rel, body in files.items():
        safe = safe_rel(rel)
        if not safe:
            continue
        dest = (root / safe).resolve()
        try:
            dest.relative_to(root.resolve())
        except ValueError:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written.append(safe)
    return written


def _tree_sketch(root: Path) -> str:
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in EXCLUDE for part in path.relative_to(root).parts):
            continue
        if len(lines) >= 24:
            lines.append("…")
            break
        lines.append(rel)
    return "\n".join(lines) or "(empty)"


def implement_prompt(
    text: str,
    inspection: dict[str, Any],
    root: Path,
    *,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
) -> str:
    """Ask the model for complete files, not a plan."""
    app = ""
    app_path = root / "src" / "App.tsx"
    if app_path.is_file():
        try:
            app = app_path.read_text(encoding="utf-8")[:1200]
        except OSError:
            app = ""
    retry = ""
    if retry_reason:
        retry = retry_reason.strip() + "\n"
        if previous:
            retry += f"Previous reply:\n{previous[:2000]}\n\n"
    elif previous:
        retry = (
            "Your last reply had no usable file blocks. "
            "Reply again with fenced files only.\n"
            f"Previous reply:\n{previous[:2000]}\n\n"
        )
    convo = f"{conversation.strip()}\n\n" if conversation.strip() else ""
    return (
        f"{retry}"
        "Implement the user's request now. "
        "Do not plan. Do not say the work must happen outside Local Coder. "
        "First write 2-4 sentences for the user: what changed and how the app "
        "looks or behaves now. No code, no file-path dump. "
        "Then write complete files as fenced blocks. The fence header must be the path.\n"
        "You may create or replace files under src/, electron/, public/, "
        "plus root files such as index.html or package.json. "
        "For a web UI, also write src/App.css. Make it look finished: a real page "
        "background, a type scale, spacing, and cards or columns. Do not ship a "
        "row of default pills on a blank page. "
        "Write real TypeScript when the project is TypeScript. Generics, typed "
        "useState, and Record<...> are fine. Use straight quotes, not curly quotes.\n\n"
        "```src/App.tsx\n"
        "export default function App() {\n"
        "  return <main>…</main>\n"
        "}\n"
        "```\n\n"
        f"Project: {inspection.get('name')}\n"
        f"Languages: {', '.join(inspection.get('languages') or []) or 'TypeScript'}\n"
        f"Existing files:\n{_tree_sketch(root)}\n\n"
        f"Current src/App.tsx:\n{app}\n\n"
        f"{convo}"
        f"User request:\n{text.strip()}\n"
    )


def local_complete(prompt: str) -> str:
    """One non-streaming chat completion from the owned llama.cpp server."""
    cfg = load_machine_config()
    try:
        server_start(cfg, wait_s=300)
    except ServerError:
        pass
    url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": cfg.model_alias,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write complete source files for Local Coder. "
                    "First a short plain-language summary, then fenced files. No plans."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max(256, int(cfg.max_tokens or 2048)),
        "stream": False,
    }
    try:
        with httpx.Client(timeout=cfg.gen_timeout, trust_env=False, follow_redirects=False) as client:
            response = client.post(url, json=payload)
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"The local model did not answer: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"The local model returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("The local model returned an unreadable reply.") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("The local model returned an empty reply.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "").strip()
    if not content and isinstance(choices[0], dict):
        content = str(choices[0].get("text") or "").strip()
    if not content:
        raise RuntimeError("The local model returned an empty reply.")
    return content


def complete_files(
    root: Path,
    text: str,
    inspection: dict[str, Any],
    *,
    complete: Callable[[str], str] | None = None,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
) -> tuple[list[str], str, list[str]]:
    """Ask the model once. Write only files the preview can compile."""
    prompt = implement_prompt(
        text, inspection, root, previous=previous, conversation=conversation,
        retry_reason=retry_reason,
    )
    raw = (complete or local_complete)(prompt)
    files, problems = checked_blocks(raw)
    if not files or problems:
        return [], raw, problems
    return apply_files(root, files), raw, []


def implement_files(
    root: Path,
    text: str,
    inspection: dict[str, Any],
    *,
    complete: Callable[[str], str] | None = None,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
) -> list[str]:
    """Ask the model for files and write them under root."""
    written, _raw, _problems = complete_files(
        root, text, inspection, complete=complete, previous=previous,
        conversation=conversation, retry_reason=retry_reason,
    )
    return written
