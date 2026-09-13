"""Disposable candidate workspace with transactional patching.

Protocol v1 applies unified diffs (control profile only). Protocol v2
applies exact old/new text replacements bound to runtime file IDs: the
model never computes hashes, line coordinates, hunk lengths, or headers.
Both interfaces validate everything up front, publish atomically (never
partly), and keep reverse data. Applied v2 edits are rendered back into a
deterministic unified diff in software.

A Git worktree prevents source-tree accidents; it is NOT an operating-system
sandbox. Candidate code still executes during CHECK, which is why execution
backends are a separate, explicit concern.
"""
from __future__ import annotations

import ast
import difflib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .paths import EXCLUDE_DIRS, candidate_manifest, sha256_bytes, tree_hash


class PatchValidationError(Exception):
    """A patch was rejected. Nothing was applied (all-or-nothing)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HunkLine:
    """One diff body line."""

    kind: str  # ' ', '-', '+'
    text: str


@dataclass
class Hunk:
    """One hunk with old-file coordinates."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class FilePatch:
    """All hunks for one file."""

    path: str
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class ApplyResult:
    """Outcome of a successful atomic application."""

    files: list[str]
    before: dict[str, str]
    after: dict[str, str]
    reverse: dict[str, bytes]


def _parse_hunk_header(line: str) -> Hunk:
    try:
        parts = line.split("@@")
        ranges = parts[1].strip().split()
        old = ranges[0][1:]
        new = ranges[1][1:]
        old_start, _, old_count = old.partition(",")
        new_start, _, new_count = new.partition(",")
        return Hunk(
            old_start=int(old_start),
            old_count=int(old_count) if old_count else 1,
            new_start=int(new_start),
            new_count=int(new_count) if new_count else 1,
        )
    except (IndexError, ValueError) as exc:
        raise PatchValidationError(f"malformed hunk header: {line!r}", "malformed_diff") from exc


def parse_unified_diff(diff: str) -> list[FilePatch]:
    """Parse an ordinary unified diff. No custom patch syntax accepted."""
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    old_path: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("--- "):
            old_path = raw[4:].strip()
            current = None
            hunk = None
        elif raw.startswith("+++ "):
            if old_path is None:
                raise PatchValidationError("diff has +++ without ---", "malformed_diff")
            new_path = raw[4:].strip()
            if old_path == "/dev/null" or new_path == "/dev/null":
                raise PatchValidationError("file creation/deletion is out of scope in v1", "unsupported_operation")
            for prefix in ("a/", "b/"):
                if old_path.startswith(prefix):
                    old_path = old_path[len(prefix):]
                if new_path.startswith(prefix):
                    new_path = new_path[len(prefix):]
            if old_path != new_path:
                raise PatchValidationError("renames are out of scope in v1", "unsupported_operation")
            current = FilePatch(path=new_path)
            files.append(current)
            hunk = None
        elif raw.startswith("@@ "):
            if current is None:
                raise PatchValidationError("hunk outside file patch", "malformed_diff")
            hunk = _parse_hunk_header(raw)
            current.hunks.append(hunk)
        elif raw.startswith("\\ "):
            continue
        elif raw[:1] in (" ", "-", "+") and hunk is not None and current is not None:
            hunk.lines.append(HunkLine(raw[0], raw[1:]))
        elif not raw.strip():
            continue
        else:
            raise PatchValidationError(f"unrecognized diff line: {raw!r}", "malformed_diff")
    if not files:
        raise PatchValidationError("diff contains no file patches", "empty_diff")
    for fp in files:
        if not fp.hunks:
            raise PatchValidationError(f"no hunks for {fp.path}", "empty_diff")
        for hunk in fp.hunks:
            old_lines = sum(1 for ln in hunk.lines if ln.kind in (" ", "-"))
            new_lines = sum(1 for ln in hunk.lines if ln.kind in (" ", "+"))
            if old_lines != hunk.old_count or new_lines != hunk.new_count:
                raise PatchValidationError(
                    f"hunk header/body count disagreement in {fp.path} "
                    f"(declared -{hunk.old_start},{hunk.old_count} "
                    f"+{hunk.new_start},{hunk.new_count}, body has "
                    f"{old_lines}/{new_lines}); refusing to guess",
                    "malformed_diff",
                )
    return files


def _check_path(rel: str) -> None:
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        raise PatchValidationError(f"absolute path rejected: {rel!r}", "forbidden_path")
    normalized = os.path.normpath(rel).replace("\\", "/")
    if normalized.startswith("../") or normalized == ".." or "/../" in normalized:
        raise PatchValidationError(f"traversal path rejected: {rel!r}", "forbidden_path")
    for part in normalized.split("/"):
        if part in EXCLUDE_DIRS or part in (".git",):
            raise PatchValidationError(f"path escapes editable tree: {rel!r}", "forbidden_path")


def _read_candidate(root: Path, rel: str) -> tuple[bytes, list[str], bool]:
    full = root / rel
    if full.is_symlink():
        raise PatchValidationError(f"symlink rejected: {rel!r}", "forbidden_path")
    try:
        data = full.read_bytes()
    except OSError as exc:
        raise PatchValidationError(f"cannot read {rel}: {exc}", "missing_file") from exc
    if b"\0" in data[:8192]:
        raise PatchValidationError(f"binary file rejected: {rel}", "binary_file")
    if b"\r\n" in data:
        raise PatchValidationError(f"CRLF line endings unsupported, refusing: {rel}", "unsupported_encoding")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchValidationError(f"non-UTF8 file rejected: {rel}", "unsupported_encoding") from exc
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]
    return data, lines, trailing_newline


def _apply_hunks(lines: list[str], file_patches: list[Hunk], rel: str) -> list[str]:
    out = list(lines)
    offset = 0
    for hunk in file_patches:
        at = hunk.old_start - 1 + offset
        if at < 0 or at + hunk.old_count > len(out):
            raise PatchValidationError(f"hunk out of range in {rel}", "hunk_mismatch")
        old_segment = [ln.text for ln in hunk.lines if ln.kind in (" ", "-")]
        if out[at:at + hunk.old_count] != old_segment:
            raise PatchValidationError(
                f"hunk context mismatch in {rel} at line {hunk.old_start}; refusing to fuzz-apply",
                "hunk_mismatch",
            )
        new_segment = [ln.text for ln in hunk.lines if ln.kind in (" ", "+")]
        out[at:at + hunk.old_count] = new_segment
        offset += len(new_segment) - hunk.old_count
    return out


class CandidateWorkspace:
    """Owns the disposable candidate tree. Never touches the original repo."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, repo: Path, dest: Path) -> CandidateWorkspace:
        """Copy the original tree to a disposable location.

        Symlinks are preserved as symlinks (never dereferenced): PATCH
        rejects them and retrieval never lists them, so a link pointing
        outside the tree cannot become an edit or evidence channel.
        """
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            repo,
            dest,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".local-coder"),
            symlinks=True,
        )
        return cls(dest)

    def manifest(self) -> dict[str, str]:
        """Current candidate file identities."""
        return candidate_manifest(self.root)

    def tree_hash(self) -> str:
        """Current candidate tree identity."""
        return tree_hash(self.manifest())

    def read_range(self, rel: str, start: int, end: int, max_chars: int = 4000) -> str:
        """Numbered source excerpt for briefing rendering."""
        _, lines, _ = _read_candidate(self.root, rel)
        total = len(lines)
        start = max(1, start)
        end = min(total, end)
        numbered = [f"{n}:{lines[n - 1]}" for n in range(start, end + 1)]
        text = "\n".join(numbered)
        return text[:max_chars]

    def apply_diff(
        self,
        diff: str,
        focus_paths: set[str],
        editable: list[str],
        max_bytes: int,
    ) -> ApplyResult:
        """Validate everything, then apply atomically. Never partly applies."""
        from .paths import matches_any  # local import to avoid cycle

        if len(diff.encode("utf-8")) > max_bytes:
            raise PatchValidationError(f"patch exceeds {max_bytes} byte budget", "patch_too_large")
        file_patches = parse_unified_diff(diff)
        for fp in file_patches:
            _check_path(fp.path)
            if fp.path not in focus_paths:
                raise PatchValidationError(f"{fp.path} is not in the current focus set", "not_in_focus")
            if not matches_any(fp.path, editable):
                raise PatchValidationError(f"{fp.path} is not editable (tests/config are read-only)", "forbidden_path")
        planned: dict[str, tuple[bytes, bytes]] = {}
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for fp in file_patches:
            data, lines, trailing = _read_candidate(self.root, fp.path)
            new_lines = _apply_hunks(lines, fp.hunks, fp.path)
            new_text = "\n".join(new_lines) + ("\n" if trailing else "")
            new_data = new_text.encode("utf-8")
            if fp.path.endswith(".py"):
                try:
                    ast.parse(new_text, filename=fp.path)
                except SyntaxError as exc:
                    raise PatchValidationError(f"result has invalid Python: {exc}", "syntax_error") from exc
            planned[fp.path] = (data, new_data)
            before[fp.path] = sha256_bytes(data)
            after[fp.path] = sha256_bytes(new_data)
        reverse = _publish_planned(self.root, planned)
        return ApplyResult(files=sorted(planned), before=before, after=after, reverse=reverse)

    def revert_files(self, reverse: dict[str, bytes], expected_after: dict[str, str]) -> None:
        """Restore pre-patch bytes after verifying the current after-state."""
        for rel, want in expected_after.items():
            data, _, _ = _read_candidate(self.root, rel)
            if sha256_bytes(data) != want:
                raise PatchValidationError(f"unsafe revert: {rel} changed since patch", "unsafe_revert")
        for rel, old_data in reverse.items():
            dest = self.root / rel
            tmp = dest.with_suffix(dest.suffix + ".reverttmp")
            tmp.write_bytes(old_data)
            os.replace(tmp, dest)

    def apply_replacements(
        self,
        edits: list[tuple[str, str, str]],
        file_hashes: dict[str, str],
        focus_paths: set[str],
        editable: list[str],
        max_bytes: int,
    ) -> ApplyResult:
        """Apply exact (path, old, new) replacements atomically.

        Every edit validates against the same pre-action file bytes: stale
        source identities, zero/ambiguous matches, no-ops, and overlapping
        replacements all reject before anything is written. On success the
        complete set publishes transactionally; a crash mid-write can leave
        at most one temp file behind, never a partial edit accepted (files
        are renamed into place only after their full contents are staged).
        """
        from .paths import matches_any  # local import to avoid cycle

        payload_bytes = sum(len(old.encode("utf-8")) + len(new.encode("utf-8")) for _, old, new in edits)
        if payload_bytes > max_bytes:
            raise PatchValidationError(f"replacement payload exceeds {max_bytes} byte budget", "patch_too_large")
        if not edits:
            raise PatchValidationError("replacement set is empty", "empty_diff")
        # Group edits by resolved path, preserving action order within a file.
        by_path: dict[str, list[tuple[str, str]]] = {}
        for rel, old, new in edits:
            _check_path(rel)
            if rel not in focus_paths:
                raise PatchValidationError(f"{rel} is not in the current focus set", "not_in_focus")
            if not matches_any(rel, editable):
                raise PatchValidationError(f"{rel} is not editable (tests/config are read-only)", "forbidden_path")
            if not old:
                raise PatchValidationError(f"empty old text rejected for {rel}", "empty_old_text")
            by_path.setdefault(rel, []).append((old, new))
        planned: dict[str, tuple[bytes, bytes]] = {}
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for rel, file_edits in by_path.items():
            data, _, _ = _read_candidate(self.root, rel)
            if sha256_bytes(data) != file_hashes.get(rel, ""):
                raise PatchValidationError(f"stale source identity for {rel}", "stale_evidence")
            text = data.decode("utf-8")
            spans: list[tuple[int, int]] = []
            for old, new in file_edits:
                if old == new:
                    raise PatchValidationError(f"no-op replacement rejected for {rel}", "noop_edit")
                first = text.find(old)
                if first < 0:
                    raise PatchValidationError(f"old text not found in {rel}", "old_text_missing")
                if text.find(old, first + 1) >= 0:
                    raise PatchValidationError(f"old text ambiguous in {rel}", "old_text_ambiguous")
                spans.append((first, first + len(old)))
            ordered = sorted(spans)
            for (_start_a, end_a), (start_b, _end_b) in zip(ordered, ordered[1:], strict=False):
                if start_b < end_a:
                    raise PatchValidationError(f"overlapping replacements in {rel}", "overlapping_edits")
            new_text = text
            for index in _span_order(spans, reverse=True):
                old, new = file_edits[index]
                start, _end = spans[index]
                new_text = new_text[:start] + new + new_text[start + len(old):]
            new_data = new_text.encode("utf-8")
            if rel.endswith(".py"):
                try:
                    ast.parse(new_text, filename=rel)
                except SyntaxError as exc:
                    raise PatchValidationError(f"result has invalid Python: {exc}", "syntax_error") from exc
            planned[rel] = (data, new_data)
            before[rel] = sha256_bytes(data)
            after[rel] = sha256_bytes(new_data)
        reverse = _publish_planned(self.root, planned)
        return ApplyResult(files=sorted(planned), before=before, after=after, reverse=reverse)


def _publish_planned(root: Path, planned: dict[str, tuple[bytes, bytes]]) -> dict[str, bytes]:
    """Stage all new contents, then rename every file into place.

    A crash during staging leaves the tree untouched (only temp files
    remain). A crash during the rename loop triggers a best-effort
    rollback of already-published files before the failure surfaces, so
    no partial edit is ever accepted as applied.
    """
    reverse: dict[str, bytes] = {}
    staged: dict[str, Path] = {}
    try:
        for rel, (old_data, new_data) in planned.items():
            dest = root / rel
            tmp = dest.with_suffix(dest.suffix + ".patchtmp")
            tmp.write_bytes(new_data)
            staged[rel] = tmp
            reverse[rel] = old_data
    except OSError as exc:
        for tmp in staged.values():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise PatchValidationError(f"could not stage replacement: {exc}", "write_failed") from exc
    published: list[str] = []
    try:
        for rel in planned:
            os.replace(staged[rel], root / rel)
            published.append(rel)
    except OSError as exc:
        for rel in published:
            try:
                dest = root / rel
                tmp = dest.with_suffix(dest.suffix + ".patchtmp")
                tmp.write_bytes(reverse[rel])
                os.replace(tmp, dest)
            except OSError:
                pass
        raise PatchValidationError(
            f"publish interrupted; rolled back {len(published)} file(s): {exc}", "write_failed") from exc
    return reverse


def _span_order(spans: list[tuple[int, int]], reverse: bool = False) -> list[int]:
    """Indices of spans sorted by start offset (stable, deterministic)."""
    return sorted(range(len(spans)), key=lambda i: spans[i][0], reverse=reverse)


def render_unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Render a deterministic unified diff from before/after file texts."""
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old_text = before.get(rel, "")
        new_text = after.get(rel, "")
        if old_text == new_text:
            continue
        chunks.extend(
            difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
            )
        )
    return "\n".join(chunks) + ("\n" if chunks else "")
