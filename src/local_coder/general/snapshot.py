"""Working-tree snapshots and B→C acceptance onto the current user tree.

Three states:
- B: user tree at request start (tracked edits and relevant untracked files)
- C: finished candidate produced by the engine
- U: current user tree at Keep time

Only the delta B→C is applied. A touched path that changed between B and U
is a conflict. Unrelated user edits survive. Newly created candidate paths
that now exist in U are also conflicts.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from local_coder.paths import EXCLUDE_DIRS, EXCLUDE_GLOBS, sha256_file

EXTRA_EXCLUDE_DIRS = {
    ".local-coder",
    ".opencode",
    "coverage",
    ".turbo",
    ".next",
    ".vite",
    "playwright-report",
    "test-results",
}
EXCLUDE = set(EXCLUDE_DIRS) | EXTRA_EXCLUDE_DIRS
SECRET_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
NOISE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
}


@dataclass
class FileEntry:
    """One snapshotted file with a content identity."""

    rel: str
    sha256: str
    mode: int
    size: int


@dataclass
class TreeIndex:
    """Content identities for a tree."""

    root: Path
    files: dict[str, FileEntry] = field(default_factory=dict)

    def sha_of(self, rel: str) -> str | None:
        entry = self.files.get(rel)
        return entry.sha256 if entry else None


@dataclass
class Delta:
    """B→C change set."""

    added: list[str]
    modified: list[str]
    deleted: list[str]
    renamed: list[tuple[str, str]]

    def touched(self) -> set[str]:
        names = set(self.added) | set(self.modified) | set(self.deleted)
        for old, new in self.renamed:
            names.add(old)
            names.add(new)
        return names

    def as_dict(self) -> dict[str, object]:
        return {
            "added": list(self.added),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "renamed": [{"from": a, "to": b} for a, b in self.renamed],
        }


@dataclass
class Conflict:
    """A path that cannot be applied blindly."""

    path: str
    reason: str


def is_excluded(rel: str) -> bool:
    """Whether a relative path is omitted from snapshots."""
    parts = Path(rel).parts
    if any(part in EXCLUDE or part.endswith(".egg-info") for part in parts):
        return True
    name = Path(rel).name
    if name in SECRET_NAMES or name in NOISE_NAMES:
        return True
    if name.endswith(".pem") or name.endswith(".key"):
        return True
    from fnmatch import fnmatch

    for pattern in EXCLUDE_GLOBS:
        if fnmatch(name, pattern) or fnmatch(rel, pattern):
            return True
    return False


def iter_source_files(root: Path) -> list[Path]:
    """Relevant project files, including untracked source."""
    files: list[Path] = []
    if not root.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [
            name for name in dirnames
            if name not in EXCLUDE and not name.endswith(".egg-info")
            and not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}".replace("\\", "/")
            if is_excluded(rel):
                continue
            if not full.is_file():
                continue
            files.append(full)
    return files


def index_tree(root: Path) -> TreeIndex:
    """Build content identities for a tree."""
    index = TreeIndex(root=root)
    for full in iter_source_files(root):
        rel = str(full.relative_to(root)).replace("\\", "/")
        try:
            mode = full.stat().st_mode
            index.files[rel] = FileEntry(
                rel=rel, sha256=sha256_file(full), mode=mode, size=full.stat().st_size,
            )
        except OSError:
            continue
    return index


def copy_snapshot(source: Path, dest: Path) -> TreeIndex:
    """Copy the current working tree into dest. Does not reset to git HEAD."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for full in iter_source_files(source):
        rel = full.relative_to(source)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(full, target)
    return index_tree(dest)


def clone_snapshot(source: Path, dest: Path) -> TreeIndex:
    """Duplicate an existing snapshot quickly (hardlinks when the volume allows)."""
    if dest.exists():
        shutil.rmtree(dest)
    try:
        shutil.copytree(source, dest, copy_function=os.link)
    except OSError:
        shutil.copytree(source, dest, copy_function=shutil.copy2)
    return index_tree(dest)


def compute_delta(before: TreeIndex, after: TreeIndex) -> Delta:
    """Describe B→C using content identities. Detect simple renames."""
    old = before.files
    new = after.files
    old_paths = set(old)
    new_paths = set(new)
    added = sorted(new_paths - old_paths)
    deleted = sorted(old_paths - new_paths)
    modified = sorted(
        path for path in old_paths & new_paths if old[path].sha256 != new[path].sha256
    )
    renamed: list[tuple[str, str]] = []
    unused_deleted = list(deleted)
    unused_added = list(added)
    used_old: set[str] = set()
    used_new: set[str] = set()
    for old_path in unused_deleted:
        digest = old[old_path].sha256
        matches = [
            new_path for new_path in unused_added
            if new[new_path].sha256 == digest and new_path not in used_new
        ]
        if len(matches) == 1:
            renamed.append((old_path, matches[0]))
            used_old.add(old_path)
            used_new.add(matches[0])
    added = [path for path in added if path not in used_new]
    deleted = [path for path in deleted if path not in used_old]
    return Delta(added=added, modified=modified, deleted=deleted, renamed=renamed)


def find_conflicts(baseline: TreeIndex, candidate: TreeIndex, user: TreeIndex) -> list[Conflict]:
    """Conflicts between B→C and the current user tree U."""
    delta = compute_delta(baseline, candidate)
    conflicts: list[Conflict] = []
    for rel in delta.touched():
        b = baseline.sha_of(rel)
        u = user.sha_of(rel)
        if rel in delta.added and u is not None and u != candidate.sha_of(rel):
            conflicts.append(Conflict(rel, "a new file now exists in the project"))
            continue
        if rel in delta.added:
            continue
        if b is not None and u is not None and u != b:
            conflicts.append(Conflict(rel, "this file changed in the project after the request started"))
        elif b is not None and u is None and rel not in {old for old, _ in delta.renamed}:
            conflicts.append(Conflict(rel, "this file was removed from the project after the request started"))
    return conflicts


def unified_diff(baseline: Path, candidate: Path, *, limit: int = 200_000) -> str:
    """Software-owned unified diff of B→C."""
    import difflib

    delta = compute_delta(index_tree(baseline), index_tree(candidate))
    chunks: list[str] = []
    for rel in delta.modified + delta.added:
        old_lines = _read_lines(baseline / rel) if (baseline / rel).is_file() else []
        new_lines = _read_lines(candidate / rel) if (candidate / rel).is_file() else []
        pieces = list(difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
        ))
        if pieces:
            chunks.append("\n".join(pieces))
    for rel in delta.deleted:
        old_lines = _read_lines(baseline / rel)
        pieces = list(difflib.unified_diff(
            old_lines, [], fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
        ))
        if pieces:
            chunks.append("\n".join(pieces))
    for old, new in delta.renamed:
        chunks.append(f"rename: {old} -> {new}")
    text = "\n".join(chunks)
    if len(text) > limit:
        return text[:limit] + "\n… (diff truncated)\n"
    return text


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ["<binary or unreadable>\n"]


def apply_delta(user: Path, baseline: Path, candidate: Path) -> dict[str, object]:
    """Apply B→C onto U transactionally. Refuses on conflict or path escape."""
    user = user.resolve()
    baseline = baseline.resolve()
    candidate = candidate.resolve()
    b_index = index_tree(baseline)
    c_index = index_tree(candidate)
    u_index = index_tree(user)
    conflicts = find_conflicts(b_index, c_index, u_index)
    if conflicts:
        return {
            "ok": False,
            "code": "conflict",
            "detail": "Some files changed in the project while this request was running.",
            "conflicts": [{"path": row.path, "reason": row.reason} for row in conflicts],
            "applied": [],
        }
    delta = compute_delta(b_index, c_index)
    journal: dict[str, bytes | None] = {}
    applied: list[str] = []
    try:
        for rel in delta.deleted:
            _assert_inside(user, rel)
            dst = user / rel
            journal[rel] = dst.read_bytes() if dst.exists() else None
            if dst.exists():
                dst.unlink()
            applied.append(rel)
        for old, new in delta.renamed:
            _assert_inside(user, old)
            _assert_inside(user, new)
            src = user / old
            dst = user / new
            journal[old] = src.read_bytes() if src.exists() else None
            journal[new] = dst.read_bytes() if dst.exists() else None
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                os.replace(src, dst)
            applied.extend([old, new])
        for rel in delta.added + delta.modified:
            _assert_inside(user, rel)
            _assert_inside(candidate, rel)
            src = candidate / rel
            dst = user / rel
            if src.is_symlink() or dst.is_symlink():
                raise OSError(f"refusing symlink: {rel}")
            journal[rel] = dst.read_bytes() if dst.exists() else None
            dst.parent.mkdir(parents=True, exist_ok=True)
            _atomic_copy(src, dst)
            applied.append(rel)
    except OSError as exc:
        _rollback(user, journal)
        return {"ok": False, "code": "apply_failed", "detail": str(exc), "applied": []}
    return {"ok": True, "applied": sorted(set(applied)), "delta": delta.as_dict()}


def _assert_inside(root: Path, rel: str) -> None:
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise OSError(f"path escape: {rel}")
    resolved = (root / rel).resolve()
    if not str(resolved).startswith(str(root.resolve()) + os.sep) and resolved != root.resolve():
        raise OSError(f"path escape: {rel}")


def _atomic_copy(src: Path, dst: Path) -> None:
    mode = src.stat().st_mode
    with tempfile.NamedTemporaryFile(delete=False, dir=str(dst.parent)) as handle:
        tmp = Path(handle.name)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, stat.S_IMODE(mode))
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _rollback(user: Path, journal: dict[str, bytes | None]) -> None:
    for rel, original in journal.items():
        dst = user / rel
        try:
            if original is None:
                dst.unlink(missing_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(original)
        except OSError:
            continue


def write_index(path: Path, index: TreeIndex) -> None:
    """Persist a tree index for later conflict checks."""
    payload = {
        "root": str(index.root),
        "files": {
            rel: {"sha256": entry.sha256, "mode": entry.mode, "size": entry.size}
            for rel, entry in sorted(index.files.items())
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_index(path: Path) -> TreeIndex:
    """Load a frozen tree index written by write_index."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    root = Path(str(payload.get("root") or "."))
    index = TreeIndex(root=root)
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    for rel, row in files.items():
        if not isinstance(row, dict):
            continue
        index.files[str(rel)] = FileEntry(
            rel=str(rel),
            sha256=str(row.get("sha256") or ""),
            mode=int(row.get("mode") or 0),
            size=int(row.get("size") or 0),
        )
    return index
