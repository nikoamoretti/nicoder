"""Path policy: inventory, exclusions, content identity.

Retrieval sees permitted source and test paths only. Secrets, dependency
trees, generated output, Git metadata, runtime state, and hidden
benchmark material are excluded before any ranking happens.
"""
from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", ".tox",
    "node_modules", "dist", "build", "eggs", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".local-coder", ".hypothesis",
}

EXCLUDE_GLOBS = [
    "*.egg-info", "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll",
    "*.db", "*.sqlite3", "*.pem", "*.key", "*.p12", "*.pfx",
    ".env", ".env.*", "*credentials*", "*secret*",
    "*.gold.*", "*.gold", "*hidden_eval*", "*gold_patch*",
]

# Test-runner byproducts: never retrieval evidence, never tamper signal.
BYPRODUCT_GLOBS = [
    ".coverage", ".coverage.*", "coverage.xml", "htmlcov/**",
    "nosetests.xml", "test-results/**", "*.log",
]

MAX_FILE_BYTES = 200_000


def is_byproduct(rel: str) -> bool:
    """Whether a path is an approved test-runner byproduct."""
    return matches_any(rel, BYPRODUCT_GLOBS)


def _excluded(rel: str, name: str) -> bool:
    for part in rel.split("/"):
        if part in EXCLUDE_DIRS:
            return True
        if part.endswith(".egg-info"):
            return True
    for pattern in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def is_binary(path: Path) -> bool:
    """Detect binary files via a null byte in the leading chunk."""
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(8192)
    except OSError:
        return True


def load_gitignore(repo: Path) -> list[str]:
    """Read a target repo's .gitignore as a documented pattern subset.

    Supports blank lines, `#` comments, trailing-`/` directory prefixes, and
    `*`/`?` wildcards. `!` negations are not supported and are ignored openly
    (returned separately would be better; v1 documents the subset instead).
    """
    path = repo / ".gitignore"
    patterns: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return patterns
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        patterns.append(stripped.lstrip("/"))
    return patterns


def _gitignored(rel: str, name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/"):
            prefix = pattern
            if rel == prefix.rstrip("/") or rel.startswith(prefix):
                return True
            continue
        if "/" in pattern:
            if fnmatch.fnmatch(rel, pattern):
                return True
            continue
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def build_inventory(
    repo: Path,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    gitignore: list[str] | None = None,
) -> list[str]:
    """List permitted relative paths, sorted.

    Builtin excludes (secrets, deps, state, gold material) always apply, then
    the repo's own .gitignore, then the explicit allow/deny policy: a path
    must match `allow` (default all) and must not match `deny`.
    """
    allow = ["**"] if allow is None else allow
    deny = [] if deny is None else deny
    gitignore = load_gitignore(repo) if gitignore is None else gitignore
    found: list[str] = []
    for entry in sorted(repo.rglob("*")):
        if entry.is_dir() or entry.is_symlink():
            continue
        try:
            rel = entry.relative_to(repo).as_posix()
        except ValueError:
            continue
        if _excluded(rel, entry.name):
            continue
        if is_byproduct(rel):
            continue
        if _gitignored(rel, entry.name, gitignore):
            continue
        if not matches_any(rel, allow):
            continue
        if matches_any(rel, deny):
            continue
        try:
            if entry.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        if is_binary(entry):
            continue
        found.append(rel)
    return found


def matches_any(rel: str, globs: list[str]) -> bool:
    """Match a repo-relative path against glob patterns.

    `**` spans zero or more directories, so `src/**/*.py` matches both
    `src/calc.py` and `src/sub/calc.py`.
    """
    for pattern in globs:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if "**/" in pattern and fnmatch.fnmatch(rel, pattern.replace("**/", "")):
            return True
    return False


def is_editable(rel: str, editable: list[str]) -> bool:
    """Whether a path may be modified by PATCH."""
    return matches_any(rel, editable)


def sha256_bytes(data: bytes) -> str:
    """Hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex digest of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_hashes(root: Path, files: list[str]) -> dict[str, str]:
    """Map each relative path to its content hash."""
    out: dict[str, str] = {}
    for rel in files:
        full = root / rel
        if full.is_file() and not full.is_symlink():
            try:
                out[rel] = sha256_file(full)
            except OSError:
                continue
    return out


def tree_hash(manifest: dict[str, str]) -> str:
    """Hash of the whole source tree identity."""
    canonical = "\n".join(f"{path}\0{digest}" for path, digest in sorted(manifest.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def candidate_manifest(root: Path) -> dict[str, str]:
    """Manifest of every non-excluded file under a candidate tree."""
    files: list[str] = []
    for entry in sorted(root.rglob("*")):
        if entry.is_dir() or entry.is_symlink():
            continue
        rel = entry.relative_to(root).as_posix()
        if _excluded(rel, entry.name):
            continue
        if is_byproduct(rel):
            continue
        try:
            if entry.stat().st_size > MAX_FILE_BYTES or is_binary(entry):
                continue
        except OSError:
            continue
        files.append(rel)
    return manifest_hashes(root, files)


def original_manifest(repo: Path) -> dict[str, str]:
    """Manifest of the developer's original source tree."""
    return manifest_hashes(repo, build_inventory(repo))
