"""Working-tree snapshots and B→C acceptance onto the current user tree.

Three states:
- B: user tree at request start (tracked edits and relevant untracked files)
- C: finished candidate produced by the engine
- U: current user tree at Keep time

Only the delta B→C is applied. A touched path that changed between B and U
is a conflict. Unrelated user edits survive. Newly created candidate paths
that now exist in U are also conflicts, and so is a rename's destination when
U holds other text there than the request wrote. A rename is checked at both
ends: its source must still be there unchanged, or the move must already have
been made by hand. Found before the first write: a result that lists no files
while its baseline lists some (Keep would empty the project), a result whose
`candidate/` folder is gone (whatever the baseline lists), a clash at either
end of a rename, a link, a folder where a file goes or where a rename's source
was, a plain file or a link that is not a folder where a folder must be made
(unless this Keep removes it first), a looping link, a folder he cannot search
and a path leaving the project. Found only while writing (then rolled back,
bytes only, so a deleted file comes back with mode 0644): a disk error. Only a
clash refusal names the files; the others keep the generic sentence.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from local_coder.paths import EXCLUDE_DIRS, EXCLUDE_GLOBS, sha256_file

EXTRA_EXCLUDE_DIRS = {
    # Scratch the agent's own tools leave in the working copy (seen: 696 files of browsers and a uv cache).
    ".agenttmp",
    ".cache",
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
COMPARE_CHUNK = 1 << 20  # bytes read from each of two files at a time when they are compared


# Why a path is a conflict. `Conflict.reason` carries one of these as it is: the MCP apply prints them, and
# `conflict_sentence` reads them back, so both sides use the same constants.
REASON_CHANGED = "this file changed in the project after the request started"
REASON_REMOVED = "this file was removed from the project after the request started"
REASON_ADDED = "a new file now exists in the project"
NAMES_SHOWN = 3
SENTENCE_HEAD = "Not kept: since these changes were started, "
SENTENCE_TAIL = "Your project was not changed."
# A result that lists no files while its baseline lists some would delete every file of the project.
EMPTY_RESULT_CODE = "empty_result"
EMPTY_RESULT_SENTENCE = (
    "This result has no files left, so Keep would empty your project. Your project was not changed."
)
# A `candidate/` folder that is gone has nothing to keep, whatever the baseline lists. The guard above needs a baseline
# that lists files, and a first request on an empty folder has none: the gone folder would read as a result that
# changed nothing, and Keep would answer ok with nothing written and mark the request kept.
RESULT_GONE_CODE = "result_gone"
RESULT_GONE_SENTENCE = (
    "This result's working copy is gone, so there is nothing to keep. Your project was not changed."
)
# (reason, how the sentence says it), in the order the clauses are written.
_CLAUSES = (
    (REASON_CHANGED, "changed in"),
    (REASON_REMOVED, "removed from"),
    (REASON_ADDED, "added to"),
)


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


def _holds_same_bytes(source: Path, target: Path) -> bool:
    """True when target is a plain file with exactly the bytes of source.

    Compared byte by byte, every time. `filecmp.cmp` remembers its answer for a pair of files by their size and
    modification time, even with `shallow=False`: in the long-running helper an edit that keeps both (`cp -p`,
    `touch -r`) after an earlier "same" would read "same" again, and the dev server's copy would keep the old file.
    """
    try:
        if target.is_symlink() or not target.is_file() or source.stat().st_size != target.stat().st_size:
            return False
        with source.open("rb") as left, target.open("rb") as right:
            while True:
                chunk = left.read(COMPARE_CHUNK)
                if chunk != right.read(COMPARE_CHUNK):
                    return False
                if not chunk:
                    return True
    except OSError:
        return False


def sync_source_tree(source: Path, dest: Path, *, skip_same: bool = False) -> TreeIndex:
    """Copy source files onto dest without deleting dest-only dirs such as node_modules.

    With `skip_same`, a file whose bytes dest already holds is not written again: a dev server that watches dest
    sees every rewritten file as changed (and restarts when it is `vite.config.ts`), even with the same bytes.
    """
    dest.mkdir(parents=True, exist_ok=True)
    incoming = {
        str(full.relative_to(source)).replace("\\", "/"): full
        for full in iter_source_files(source)
    }
    existing = {
        str(full.relative_to(dest)).replace("\\", "/"): full
        for full in iter_source_files(dest)
    }
    for rel in set(existing) - set(incoming):
        try:
            existing[rel].unlink(missing_ok=True)
        except OSError:
            continue
    for rel, full in incoming.items():
        target = dest / rel
        if skip_same and _holds_same_bytes(full, target):
            continue
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


def would_empty_project(baseline: TreeIndex, candidate: TreeIndex) -> bool:
    """True when the baseline lists files and the candidate lists none: applying it would delete every one of them.

    One condition for every way a result can lose all its files: a `candidate/` folder that is gone, emptied or
    unreadable (`index_tree` never raises, it lists nothing), and a request that deleted every file. `apply_delta`
    asks it before any write, so Keep and the MCP apply are protected alike. It does not see a candidate that is
    missing one file or has one unreadable subfolder: the rest is listed, so those files are still deleted under a
    normal card (a later cut, `frozen-candidate-index`).
    """
    return bool(baseline.files) and not candidate.files


def find_conflicts(baseline: TreeIndex, candidate: TreeIndex, user: TreeIndex) -> list[Conflict]:
    """Conflicts between B→C and the current user tree U, sorted by path, at most one for each path.

    A rename is looked at once, at both ends (`_rename_conflicts`). Every other touched path is a conflict when
    it is added and U holds other text there, or when it changed or was removed in U since B.
    """
    delta = compute_delta(baseline, candidate)
    moves = dict(delta.renamed)
    destinations = set(moves.values())
    found: dict[str, str] = {}
    for rel in delta.touched():
        if rel in destinations:
            continue  # looked at together with its source
        if rel in moves:
            found.update(_rename_conflicts(rel, moves[rel], baseline, candidate, user))
            continue
        b = baseline.sha_of(rel)
        u = user.sha_of(rel)
        if rel in delta.added:
            if u is not None and u != candidate.sha_of(rel):
                found[rel] = REASON_ADDED
        elif b is not None and u is not None and u != b:
            found[rel] = REASON_CHANGED
        elif b is not None and u is None:
            found[rel] = REASON_REMOVED
    return [Conflict(path, found[path]) for path in sorted(found)]


def _rename_conflicts(
    old: str, new: str, baseline: TreeIndex, candidate: TreeIndex, user: TreeIndex,
) -> dict[str, str]:
    """The conflicts at the two ends of one rename, as {path: reason}.

    The destination is free, or already holds exactly the text the request wrote there; otherwise it is a file he
    made and counts as added. The source is still there unchanged, or he made the move himself (the destination
    holds the request's text); otherwise it changed, or it is gone and Keep would report a write it never made.
    Both are decided from the indexes and never from `Path.exists()`: the disk ignores case, so a rename that
    only changes the case of a name would clash with itself. The indexes skip links, so those are seen on the
    disk: a link at the destination is a file he made, one at the source is a changed file.
    """
    wrote = candidate.sha_of(new)
    at_old = user.sha_of(old)
    at_new = user.sha_of(new)
    found: dict[str, str] = {}
    if at_new is not None and at_new != wrote:
        found[new] = REASON_ADDED
    if at_old is not None and at_old != baseline.sha_of(old):
        found[old] = REASON_CHANGED
    elif at_old is None and at_new != wrote:
        found[old] = REASON_REMOVED
    if (user.root / new).is_symlink():
        found[new] = REASON_ADDED
    if (user.root / old).is_symlink():
        found[old] = REASON_CHANGED
    return found


def conflict_sentence(conflicts: Iterable[Mapping[str, object]]) -> str:
    """Nico's sentence for a refused Keep, built from the conflict list `apply_delta` returns.

    Every file is named once (a path listed twice counts once), sorted, with what happened to it: changed,
    removed or added. Past three files the first three are named and the rest counted, all as changed: in that
    long form "changed" also covers removed and added files. Project-relative paths and nothing else about the
    files, and never any text from a model.
    """
    order = [reason for reason, _verb in _CLAUSES]
    kind_of: dict[str, int] = {}
    for row in conflicts:
        path = str(row.get("path"))
        reason = str(row.get("reason"))
        kind = order.index(reason) if reason in order else 0
        kind_of[path] = min(kind, kind_of.get(path, kind))
    paths = sorted(kind_of)
    if not paths:
        return f"{SENTENCE_HEAD}some files were changed in your project. {SENTENCE_TAIL}"
    if len(paths) > NAMES_SHOWN:
        more = len(paths) - NAMES_SHOWN
        shown = f"{', '.join(paths[:NAMES_SHOWN])} and {more} more file{'' if more == 1 else 's'}"
        return f"{SENTENCE_HEAD}{shown} were changed in your project. {SENTENCE_TAIL}"
    clauses = []
    for kind, (_reason, verb) in enumerate(_CLAUSES):
        named = [path for path in paths if kind_of[path] == kind]
        if named:
            clauses.append(f"{_name_list(named)} {'was' if len(named) == 1 else 'were'} {verb} your project")
    return f"{SENTENCE_HEAD}{' and '.join(clauses)}. {SENTENCE_TAIL}"


def _name_list(names: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


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
    """Apply B→C onto U transactionally. Refuses on conflict or path escape.

    Found before the first delete or write, in one pass: a result with no files left while the baseline lists some
    (`would_empty_project`), a `candidate/` that is not a folder (gone, whatever the baseline lists: the check above
    only sees a baseline that lists files), a clash with what he changed (both ends of every rename included; looking
    at them on the disk inside a folder he cannot search is an `OSError`, refused as `apply_failed` like the rest) and
    whatever else `_blocked_before_writing` can know (a link, a folder or a file in the way, a looping link, a folder
    he cannot search, a path leaving the project). Found only while writing: a disk error (a full disk, a folder that
    is read-only). It is rolled back, but `_rollback` restores bytes only: a deleted executable comes back as a new
    file with mode 0644. The `detail` of a conflict stays as it is for the MCP apply, which prints it with the
    list; Keep builds Nico's sentence from that list (`conflict_sentence`).
    """
    user = user.resolve()
    baseline = baseline.resolve()
    candidate = candidate.resolve()
    b_index = index_tree(baseline)
    c_index = index_tree(candidate)
    if would_empty_project(b_index, c_index):
        return {"ok": False, "code": EMPTY_RESULT_CODE, "detail": EMPTY_RESULT_SENTENCE, "applied": []}
    if not candidate.is_dir():
        return {"ok": False, "code": RESULT_GONE_CODE, "detail": RESULT_GONE_SENTENCE, "applied": []}
    u_index = index_tree(user)
    try:
        conflicts = find_conflicts(b_index, c_index, u_index)
    except OSError as exc:  # a rename's two ends are looked at on the disk: inside a folder he cannot search that fails
        return {"ok": False, "code": "apply_failed", "detail": str(exc), "applied": []}
    if conflicts:
        return {
            "ok": False,
            "code": "conflict",
            "detail": "Some files changed in the project while this request was running.",
            "conflicts": [{"path": row.path, "reason": row.reason} for row in conflicts],
            "applied": [],
        }
    delta = compute_delta(b_index, c_index)
    blocked = _blocked_before_writing(user, candidate, delta)
    if blocked:
        return {"ok": False, "code": "apply_failed", "detail": blocked, "applied": []}
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
    except (OSError, RuntimeError) as exc:  # RuntimeError: `resolve()` on a link that loops (Python 3.12)
        _rollback(user, journal)
        return {"ok": False, "code": "apply_failed", "detail": str(exc), "applied": []}
    return {"ok": True, "applied": sorted(set(applied)), "delta": delta.as_dict()}


def _blocked_before_writing(user: Path, candidate: Path, delta: Delta) -> str | None:
    """The reason the writes would fail halfway, found before the first one, or None.

    A path that leaves the project (a folder of his that became a link elsewhere), a link where a file is to be
    written (never followed or replaced), and whatever else stands in the way of a write (`_in_the_way`): a folder
    where a file goes, a plain file or a link that is not a folder where a parent folder must be made, and a folder
    where a rename's source was (he made the move himself and put a folder there). The check follows the order of
    the writes: deletes, then each rename (the folder of its destination first, then the move), then the rest. So a
    file this Keep removes before it makes the folder on its name is no obstacle.

    All of it runs inside one `try`. A looping link makes `resolve()` raise RuntimeError (Python 3.12) and a folder
    he cannot search makes the checks themselves raise PermissionError: both are refusals found before the first
    write, not unhandled errors. The write loop checks the first two again at each file.
    """
    try:
        for rel in sorted(delta.touched()):
            _assert_inside(user, rel)
        for rel in delta.added + delta.modified:
            _assert_inside(candidate, rel)
        for rel in delta.added + delta.modified:
            if (candidate / rel).is_symlink() or (user / rel).is_symlink():
                return f"refusing symlink: {rel}"
        gone = {name.casefold() for name in delta.deleted}  # the first writes delete these
        for old, new in delta.renamed:  # then each rename makes the folder of its destination, then moves its source
            blocker = _in_the_way(user, new, gone)
            if blocker:
                return blocker
            gone.add(old.casefold())
        for rel in delta.added + delta.modified:  # then the added and modified files
            blocker = _in_the_way(user, rel, gone)
            if blocker:
                return blocker
        for old, _new in delta.renamed:
            if (user / old).is_dir():
                return f"refusing folder: {old}"
    except (OSError, RuntimeError) as exc:
        return str(exc)
    return None


def _in_the_way(user: Path, rel: str, gone: set[str]) -> str | None:
    """What stops `rel` from being written, or None: a folder at the path itself, or a plain file or a link that is
    not a folder (dangling or looping too) where one of its parent folders must be made.

    A parent in `gone` (names in lower case) is a file this Keep has already deleted or moved away by the time of
    this write: a file replaced by a folder of the same name. Names are compared without regard to case because the
    disk may not tell `Docs` from `docs` (macOS does not): a file replaced by a folder that only differs in case is
    the same replacement. Where the disk does tell them apart, a file of his that differs only in case from a removed
    one is left to the write, which is rolled back as before. A link to a folder is a fine parent: the write goes
    through it, and `_assert_inside` has already kept it inside the project.
    """
    if (user / rel).is_dir():
        return f"refusing folder: {rel}"
    for parent in list(Path(rel).parents)[:-1]:
        if parent.as_posix().casefold() not in gone and os.path.lexists(user / parent) and not (user / parent).is_dir():
            return f"not a folder: {parent}"
    return None


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
        # An index frozen by an older build can name a folder that is excluded now (.cache, .agenttmp): drop it, or
        # the review would list those files as deleted and the next request would stop stacking on this one.
        if not isinstance(row, dict) or is_excluded(str(rel)):
            continue
        index.files[str(rel)] = FileEntry(
            rel=str(rel),
            sha256=str(row.get("sha256") or ""),
            mode=int(row.get("mode") or 0),
            size=int(row.get("size") or 0),
        )
    return index
