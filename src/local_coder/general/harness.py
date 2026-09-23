"""Tiny candidate-only tools. Local Coder applies them; the model never owns the tree."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from local_coder.general.snapshot import EXCLUDE
from local_coder.general.source_check import scan_text

_FENCE = re.compile(r"```(?P<header>[^\n]*)\n(?P<body>.*?)```", re.S)
_THINK = re.compile(r"<think>.*?</think>", re.S)
_HASH = re.compile(r"^[0-9a-f]{8}$")
_SHELL = frozenset({"bash", "sh", "shell", "zsh", "fish", "cmd", "powershell", "pwsh"})
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
_READ_LIMIT = 8000
_HASH_LINES = 80
_TOOL_READ = re.compile(
    r"<function\s*=\s*read>\s*(?P<path>[^\s<]+)\s*</function>",
    re.I,
)


class EditError(ValueError):
    """A hash-anchored edit that does not apply to the current file."""


@dataclass
class ParsedTurn:
    """One model reply, classified into safe actions."""

    reads: list[str] = field(default_factory=list)
    writes: dict[str, str] = field(default_factory=dict)
    edits: list[tuple[str, str]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def line_hash(line: str) -> str:
    """Stable 8-hex hash of one source line, without a trailing newline."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:8]


def format_hashed(
    text: str,
    *,
    limit: int = _HASH_LINES,
    start_line: int = 1,
    mark_line: int | None = None,
) -> str:
    """Prefix each line with its hash so the model can edit without line numbers."""
    lines = text.splitlines()
    start = max(0, int(start_line) - 1)
    window = lines[start : start + limit]
    rows: list[str] = []
    if start > 0:
        rows.append(f"… ({start} lines above)")
    for offset, line in enumerate(window):
        number = start + offset + 1
        prefix = ">" if mark_line == number else ""
        rows.append(f"{prefix}{line_hash(line)}|{line}")
    leftover = len(lines) - start - len(window)
    if leftover > 0:
        rows.append(f"… ({leftover} lines below)")
    return "\n".join(rows) + ("\n" if rows else "")


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


def path_from_header(header: str) -> str | None:
    """Extract a safe path from a fence header."""
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


def parse_actions(text: str) -> ParsedTurn:
    """Classify fenced blocks. Shell fences are recorded and never executed."""
    cleaned = _THINK.sub("", text or "")
    parsed = ParsedTurn()
    seen_read: set[str] = set()
    for match in _FENCE.finditer(cleaned):
        header = (match.group("header") or "").strip()
        body = match.group("body") or ""
        if body and not body.endswith("\n"):
            body += "\n"
        kind, rel = header_kind(header)
        if kind in _SHELL:
            parsed.refused.append(f"refused {kind} block")
            continue
        if kind == "read":
            if rel and rel not in seen_read:
                parsed.reads.append(rel)
                seen_read.add(rel)
            continue
        if kind == "edit":
            if rel:
                parsed.edits.append((rel, body))
            continue
        if rel:
            parsed.writes[rel] = body
    if not parsed.reads and not parsed.writes and not parsed.edits:
        for match in _TOOL_READ.finditer(cleaned):
            rel = safe_rel(match.group("path"))
            if rel and rel not in seen_read:
                parsed.reads.append(rel)
                seen_read.add(rel)
    return parsed


def apply_edit_text(current: str, patch: str) -> str:
    """Replace a hashed span. Raises EditError when the hashes are stale."""
    hashes, additions = _parse_patch(patch)
    if not hashes:
        raise EditError("edit has no line hashes")
    lines = current.splitlines()
    hashed = [line_hash(line) for line in lines]
    start = _find_span(hashed, hashes)
    if start < 0:
        raise EditError("edit hash does not match the current file")
    end = start + len(hashes)
    updated = lines[:start] + additions + lines[end:]
    return "\n".join(updated) + "\n"


def read_files(
    root: Path,
    rels: list[str],
    *,
    around: dict[str, int] | None = None,
) -> str:
    """Return hashed contents for safe candidate paths. Missing paths are named."""
    chunks: list[str] = []
    focus = around or {}
    for rel in rels:
        safe = safe_rel(rel)
        if not safe:
            continue
        path = root / safe
        if not path.is_file():
            chunks.append(f"{rel}: file is missing from the candidate")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            chunks.append(f"{safe}: file could not be read")
            continue
        mark = focus.get(safe)
        if mark:
            start = max(1, int(mark) - 24)
            chunks.append(
                f"{safe}\n{format_hashed(text, start_line=start, limit=49, mark_line=int(mark))}"
            )
            continue
        chunks.append(f"{safe}\n{format_hashed(text)}")
    return "\n".join(chunks)


def materialize(root: Path, parsed: ParsedTurn) -> tuple[dict[str, str], list[str]]:
    """Build replacement file bodies in memory. Scan before any write."""
    files: dict[str, str] = {}
    problems: list[str] = []
    for rel, patch in parsed.edits:
        if rel in parsed.writes:
            continue
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: edit target does not exist")
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{rel}: {exc}")
            continue
        try:
            files[rel] = apply_edit_text(current, patch)
        except EditError as exc:
            problems.append(f"{rel}: {exc}")
    files.update(parsed.writes)
    for rel, body in files.items():
        problems.extend(scan_text(rel, body))
    return files, problems


def header_kind(header: str) -> tuple[str, str | None]:
    """Return (kind, safe path) for a fence header."""
    first = header.split(None, 1)[0].lower() if header else ""
    first = first.split(":", 1)[0]
    if first in _SHELL:
        return first, None
    if first in {"read", "edit", "write"}:
        rest = ""
        if " " in header:
            rest = header.split(None, 1)[1]
        elif ":" in header:
            rest = header.split(":", 1)[1]
        return first, path_from_header(rest) if rest else None
    return "write", path_from_header(header)


def strip_think(text: str) -> str:
    """Remove model think tags."""
    return _THINK.sub("", text or "")


def iter_fences(text: str):
    """Yield fence matches from model text."""
    yield from _FENCE.finditer(text)


def _parse_patch(patch: str) -> tuple[list[str], list[str]]:
    hashes: list[str] = []
    additions: list[str] = []
    for raw in patch.splitlines():
        if raw.startswith("-"):
            token = raw[1:].strip().split()[0] if raw[1:].strip() else ""
            if _HASH.fullmatch(token):
                hashes.append(token)
            continue
        if raw.startswith("+"):
            additions.append(raw[1:])
    return hashes, additions


def _find_span(hashed: list[str], needle: list[str]) -> int:
    size = len(needle)
    if size == 0 or size > len(hashed):
        return -1
    for index in range(0, len(hashed) - size + 1):
        if hashed[index : index + size] == needle:
            return index
    return -1
