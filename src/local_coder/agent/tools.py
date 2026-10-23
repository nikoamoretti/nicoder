"""The agent's tools, run by Local Coder: list, glob, grep, read, write, edit, bash, open_project.

Every path goes through AccessPolicy and every command through the sandbox.
A tool never raises: refusals and failures come back as text the model can
act on, plus one short activity line for the app's Actions list.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .access import AccessDenied, AccessPolicy, secret_name
from .sandbox import available as sandbox_available
from .sandbox import run_command

PRUNE_DIRS = frozenset({
    "node_modules", ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".next", ".nuxt", ".turbo", ".cache", ".parcel-cache", ".svelte-kit", "dist",
    "build", ".build", "DerivedData", "Pods", ".gradle", "target", "coverage", ".idea", "Library", ".Trash",
})
VISIBLE_HIDDEN_DIRS = frozenset({".github"})
PROJECT_MARKERS = (
    ".git", "package.json", "pyproject.toml", "setup.py", "requirements.txt", "Package.swift", "Cargo.toml",
    "go.mod", "Gemfile", "pom.xml", "build.gradle", "composer.json", "deno.json", "project.yml", "Podfile",
    "Makefile", "index.html",
)
PROJECT_ROOTS = (
    ("projects", 2),
    ("Local Coder Projects", 1),
    ("Downloads", 1),
    ("Desktop", 1),
    ("Documents", 1),
    ("", 1),
)
_STOP_WORDS = frozenset({
    "the", "and", "app", "apps", "repo", "repos", "repository", "project", "projects", "code", "codebase",
    "folder", "mac", "macbook", "my", "on", "for", "with", "containing", "contains", "called", "named",
})
PROJECT_RESULTS = 8
READ_LINES = 2000
READ_CHARS = 30_000
LINE_CHARS = 2000
READ_MAX_BYTES = 20_000_000
GREP_FILE_BYTES = 1_000_000
GREP_MATCHES = 100
GLOB_RESULTS = 100
LIST_ENTRIES = 300
WALK_FILES = 50_000
WALK_SECONDS = 15.0
RESULT_CHARS = 30_000
_ALIASES = {
    "filePath": "path", "file_path": "path", "filepath": "path", "file": "path", "dir": "path",
    "directory": "path", "folder": "path", "oldString": "old_string", "newString": "new_string",
    "replaceAll": "replace_all", "regex": "pattern", "cmd": "command", "timeout": "timeout_seconds",
}
_BRACES = re.compile(r"\{([^{}]*)\}")


@dataclass
class ToolResult:
    """Text for the model, one activity line for the user, and what the step touched."""

    text: str
    activity: str
    ok: bool = True
    target: str = ""


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _str(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _int(description: str) -> dict[str, str]:
    return {"type": "integer", "description": description}


def expand_braces(pattern: str) -> list[str]:
    """'*.{ts,tsx}' -> ['*.ts', '*.tsx'] (one level is enough for file patterns)."""
    match = _BRACES.search(pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end():]
    return [out for option in match.group(1).split(",") for out in expand_braces(head + option + tail)]


def glob_match(rel: str, pattern: str) -> bool:
    """Match a root-relative POSIX path, ignoring case like the Mac's file system.

    A pattern without '/' matches the name at any depth.
    """
    text = pattern.strip()
    if text.startswith("./"):
        text = text[2:]
    rel = rel.lower()
    for option in expand_braces(text.lower() or "*"):
        if "/" not in option:
            if fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], option):
                return True
        elif fnmatch.fnmatchcase(rel, option) or (
            option.startswith("**/") and fnmatch.fnmatchcase(rel, option[3:])
        ):
            return True
    return False


def _exists(path: Path) -> bool:
    """Path.exists that treats a folder macOS keeps private (EACCES) as absent instead of raising."""
    try:
        return path.exists()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _readme_title(text: str) -> str:
    """First heading, else first plain line, of a README (no HTML or badge lines)."""
    rows = [row.strip() for row in text.splitlines()[:40] if row.strip()]
    plain = [row for row in rows if not row.startswith(("<", "!", "[", "```", "---"))]
    heading = next((row.lstrip("#").strip() for row in plain if row.startswith("#")), "")
    title = heading or (plain[0] if plain else "")
    return re.sub(r"<[^>]+>", "", title).strip()[:100]


def is_empty_starter(path: Path) -> bool:
    """True for a Local Coder starter nobody has built anything in yet."""
    for rel in ("src/App.tsx", "src/App.jsx"):
        try:
            text = (path / rel).read_text(encoding="utf-8", errors="replace")[:4000].lower()
        except OSError:
            continue
        return "empty on purpose" in text or "<h1>new app</h1>" in text
    return False


def inside_data_root(path: Path) -> bool:
    """True for Local Coder's own data folder or anything in it (the waiting copies live there)."""
    from local_coder.engines.opencode_config import data_root

    root = Path(os.path.realpath(data_root()))
    real = Path(os.path.realpath(path))
    for candidate in [real, *real.parents]:
        if candidate == root:
            return True
        try:
            if os.path.samefile(candidate, root):
                return True
        except OSError:
            continue
    return False


def project_root(path: Path, home: Path) -> Path | None:
    """The repo (or, without git, the project folder) a file belongs to, below the home folder."""
    folders = [folder for folder in path.parents if home in folder.parents]
    for folder in folders:
        if _exists(folder / ".git"):
            return folder
    return next((folder for folder in folders if looks_like_project(folder)), None)


def looks_like_project(path: Path) -> bool:
    """A folder with a repo or a project file, not just any folder."""
    if any(_exists(path / marker) for marker in PROJECT_MARKERS):
        return True
    try:
        return any(child.suffix in {".xcodeproj", ".xcworkspace"} for child in path.iterdir())
    except OSError:
        return False


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(8192)
    except OSError:
        return True


def _short(text: str, limit: int = 90) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _parse_args(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(arguments, dict):
        raw = arguments
    else:
        text = (arguments or "").strip()
        raw = json.loads(text) if text else {}
        if not isinstance(raw, dict):
            raise ValueError("arguments must be a JSON object")
    return {_ALIASES.get(key, key): value for key, value in raw.items()}


class ToolBox:
    """The tools for one conversation. Records what was read and changed."""

    def __init__(
        self,
        policy: AccessPolicy,
        *,
        cwd: Path,
        run_tmp: Path,
        cancel: threading.Event | None = None,
        allow_open_project: bool = False,
        allow_find_project: bool = True,
    ) -> None:
        self.policy = policy
        self.allow_find_project = allow_find_project
        self.cwd = Path(os.path.realpath(cwd))
        self.run_tmp = run_tmp
        self.cancel = cancel
        self.allow_open_project = allow_open_project
        self.read_paths: list[str] = []
        self.read_files: list[Path] = []
        self.changed_paths: list[str] = []
        self.commands: list[str] = []
        self.opened: Path | None = None
        self._walk_cut = False

    @property
    def names(self) -> list[str]:
        """Tool names offered to the model, in a stable order."""
        names = (["find_project"] if self.allow_find_project else []) + ["list", "glob", "grep", "read"]
        if self.policy.can_write:
            names += ["write", "edit"]
        names.append("bash")
        if self.allow_open_project:
            names.append("open_project")
        return names

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style tool definitions."""
        where = "the project" if self.policy.project_real else "the home folder (~)"
        path_hint = f"Absolute, ~/…, or relative to {where}."
        if self.policy.can_write:
            shell = (
                "Run a bash command in the project's working copy. It runs in a sandbox: it can write only "
                "inside the working copy and temp folders, secrets are unreadable, and the network works "
                "(installs, builds, tests, git). Background jobs are stopped when the command ends."
            )
        else:
            shell = (
                "Run a read-only bash command (git log, git show, ls, wc, …) in a sandbox. It cannot change "
                "files outside temp folders, and secrets are unreadable."
            )
        specs = {
            "find_project": _spec(
                "find_project",
                "Find projects and repos on this Mac by name or topic. Searches ~/projects, ~/Local Coder "
                "Projects, ~/Downloads, ~/Desktop, ~/Documents and the home folder; matches the words in any "
                "order against folder names, subfolder names, package names, README files and git remotes. "
                "Use it first whenever the user mentions a project, app or repo.",
                {"query": _str("Words that name the project, e.g. 'paris shadow'.")},
                ["query"],
            ),
            "list": _spec(
                "list", "List a folder. Folders end with /.", {"path": _str(f"Folder. {path_hint}")}, [],
            ),
            "glob": _spec(
                "glob",
                "Find files and folders by name. A pattern without / matches names at any depth ('*.py', "
                "'*shadow*'); a pattern with / matches paths ('src/**/*.tsx'). Newest first. Skips "
                "node_modules, .git, build output and hidden folders.",
                {"pattern": _str("Glob pattern."), "path": _str(f"Folder to search. {path_hint}")},
                ["pattern"],
            ),
            "grep": _spec(
                "grep",
                "Search file contents with a regular expression (Python syntax; add (?i) to ignore case). "
                "Returns path:line: text. Skips node_modules, .git, build output, hidden folders and "
                "binary files.",
                {
                    "pattern": _str("Regular expression."),
                    "path": _str(f"Folder or file to search. {path_hint}"),
                    "include": _str("Optional file-name filter such as '*.ts' or '*.{ts,tsx}'."),
                },
                ["pattern"],
            ),
            "read": _spec(
                "read",
                f"Read a text file with line numbers (up to {READ_LINES} lines per call).",
                {
                    "path": _str(f"File. {path_hint}"),
                    "offset": _int("First line to show (1-based)."),
                    "limit": _int("How many lines to show."),
                },
                ["path"],
            ),
            "write": _spec(
                "write",
                "Create a file, or replace one completely, in the project's working copy.",
                {"path": _str("File path inside the project."), "content": _str("The full file content.")},
                ["path", "content"],
            ),
            "edit": _spec(
                "edit",
                "Replace exact text in a file of the project's working copy. old_string must match the "
                "file exactly (including indentation) and only once, unless replace_all is true.",
                {
                    "path": _str("File path inside the project."),
                    "old_string": _str("Exact text to replace."),
                    "new_string": _str("Replacement text."),
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
                },
                ["path", "old_string", "new_string"],
            ),
            "bash": _spec(
                "bash",
                shell,
                {
                    "command": _str("The command."),
                    "timeout_seconds": _int("Time limit in seconds (default 120, max 600)."),
                    "description": _str("Five words on what the command does."),
                },
                ["command"],
            ),
            "open_project": _spec(
                "open_project",
                "Make a folder the current project in Local Coder so the user's next requests apply to it. "
                "Call it once you have found the project or repo the user is asking about.",
                {"path": _str("The project's folder.")},
                ["path"],
            ),
        }
        return [specs[name] for name in self.names]

    def call(self, name: str, arguments: str | dict[str, Any] | None) -> ToolResult:
        """Run one tool call. Never raises."""
        if name not in self.names:
            return ToolResult(
                f"There is no tool named {name!r}. Available tools: {', '.join(self.names)}.",
                f"Tried an unknown tool ({name})",
                ok=False,
            )
        try:
            args = _parse_args(arguments)
        except ValueError as exc:
            return ToolResult(f"The arguments for {name} were not valid JSON: {exc}", f"Bad {name} call", ok=False)
        handler = getattr(self, f"_tool_{name}")
        try:
            result: ToolResult = handler(args)
        except AccessDenied as exc:
            return ToolResult(f"Refused: {exc}", f"Refused: {_short(str(exc))}", ok=False)
        except KeyError as exc:
            return ToolResult(f"{name} needs the {exc.args[0]!r} argument.", f"Bad {name} call", ok=False)
        except (OSError, ValueError, TypeError, re.error) as exc:
            return ToolResult(f"{name} failed: {exc}", f"{name} failed: {_short(str(exc))}", ok=False)
        if len(result.text) > RESULT_CHARS:
            result.text = result.text[:RESULT_CHARS] + "\n… (output cut; narrow the request)"
        return result

    def _show(self, path: Path) -> str:
        return self.policy.show(path)

    def _note_read(self, path: Path) -> None:
        shown = self._show(path)
        if shown not in self.read_paths and len(self.read_paths) < 500:
            self.read_paths.append(shown)
            self.read_files.append(path)

    def _note_change(self, path: Path) -> None:
        shown = self._show(path)
        if shown not in self.changed_paths:
            self.changed_paths.append(shown)

    def _walk(self, root: Path) -> Iterator[tuple[Path, bool]]:
        """Folders and files below root, skipping noise, hidden folders and private places."""
        deadline = time.monotonic() + WALK_SECONDS
        seen = 0
        self._walk_cut = False
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            keep = []
            for name in sorted(dirnames):
                if name in PRUNE_DIRS or (name.startswith(".") and name not in VISIBLE_HIDDEN_DIRS):
                    continue
                if self.policy.denied_reason(current / name):
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in keep:
                yield current / name, True
            for name in sorted(filenames):
                seen += 1
                if seen > WALK_FILES or time.monotonic() > deadline or (self.cancel and self.cancel.is_set()):
                    self._walk_cut = True
                    return
                if not secret_name(name):
                    yield current / name, False

    def _cut_note(self) -> str:
        if not self._walk_cut:
            return ""
        return "\n(The search stopped early because the folder is large. Search a narrower folder.)"

    def _tool_find_project(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or args.get("pattern") or "").strip()
        if not query:
            raise ValueError("the query is empty")
        words = re.findall(r"[a-z0-9]+", query.lower())
        tokens = [word for word in words if len(word) >= 3 and word not in _STOP_WORDS] or words
        deadline = time.monotonic() + WALK_SECONDS
        ranked: list[tuple[tuple[Any, ...], str]] = []
        for path in self._project_candidates():
            if time.monotonic() > deadline:
                self._walk_cut = True
                break
            try:
                row = self._score_project(path, tokens)
            except OSError:
                continue
            if row is not None:
                ranked.append(row)
        ranked.sort(key=lambda item: item[0], reverse=True)
        lines = [f"{index}. {text}" for index, (_, text) in enumerate(ranked[:PROJECT_RESULTS], start=1)]
        header = f"{len(ranked)} project(s) match “{query}”"
        body = "\n" + "\n".join(lines) if lines else "\nNo project matched. Try other words, or grep under ~/projects."
        return ToolResult(f"{header}{body}{self._cut_note()}", f"Looking for projects matching “{_short(query, 40)}”",
                          target=query)

    def _project_candidates(self) -> list[Path]:
        home = self.policy.home_real
        found: list[Path] = []
        seen: set[Path] = set()
        for rel, depth in PROJECT_ROOTS:
            root = home / rel if rel else home
            if _is_dir(root) and not self.policy.denied_reason(root):
                self._collect_projects(root, depth, found, seen)
        return found

    def _collect_projects(self, folder: Path, depth: int, found: list[Path], seen: set[Path]) -> None:
        try:
            children = sorted(child for child in folder.iterdir() if _is_dir(child))
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in PRUNE_DIRS:
                continue
            real = Path(os.path.realpath(child))
            if real in seen or self.policy.denied_reason(real):
                continue
            seen.add(real)
            if looks_like_project(real):
                found.append(real)
            elif depth > 1:
                self._collect_projects(real, depth - 1, found, seen)

    def _score_project(self, path: Path, tokens: list[str]) -> tuple[tuple[Any, ...], str] | None:
        """Rank one project against the query words; None when no word matches."""
        text, subfolders, description, remote = self._project_facts(path)
        name = path.name.lower()
        score = 0
        matched: set[str] = set()
        reasons: list[str] = []
        for token in tokens:
            if token in name:
                score += 4
                matched.add(token)
                if "folder name" not in reasons:
                    reasons.append("folder name")
            hit = next((rel for rel in subfolders if token in rel.rsplit("/", 1)[-1].lower()), None)
            if hit:
                score += 3
                matched.add(token)
                if f"subfolder {hit}" not in reasons:
                    reasons.append(f"subfolder {hit}")
            if token in text:
                score += 1
                matched.add(token)
                if "README or package text" not in reasons:
                    reasons.append("README or package text")
        if not matched:
            return None
        if len(matched) == len(tokens):
            score += 2
        starter = is_empty_starter(path)
        git = _exists(path / ".git")
        stamp = path / ".git" / "index" if (path / ".git" / "index").exists() else path
        try:
            changed = stamp.stat().st_mtime
        except OSError:
            changed = 0.0
        facts = []
        if starter:
            facts.append("empty Local Coder starter, no code yet")
        elif description:
            facts.append(description)
        if git:
            facts.append(f"git{': ' + remote if remote else ''}")
        facts.append(f"changed {time.strftime('%Y-%m-%d', time.localtime(changed))}")
        line = f"{self._show(path)} — {'; '.join(facts)}. Matched: {', '.join(reasons)}."
        key = (not starter, score, git, changed)
        return key, line

    def _project_facts(self, path: Path) -> tuple[str, list[str], str, str]:
        """(searchable text, subfolder paths, one-line description, git remote) for a project."""
        parts: list[str] = []
        description = ""
        package = path / "package.json"
        if package.is_file():
            try:
                data = json.loads(package.read_text(encoding="utf-8", errors="replace")[:200_000])
            except ValueError:
                data = {}
            if isinstance(data, dict):
                parts += [str(data.get("name") or ""), str(data.get("description") or "")]
                description = str(data.get("description") or "")
        for name in ("README.md", "README", "readme.md", "README.txt", "pyproject.toml"):
            candidate = path / name
            if candidate.is_file() and not secret_name(name):
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")[:20_000]
                except OSError:
                    continue
                parts.append(text)
                if not description and name.lower().startswith("readme"):
                    description = _readme_title(text)
        remote = ""
        git_config = path / ".git" / "config"
        if git_config.is_file():
            try:
                urls = re.findall(r"url\s*=\s*(\S+)", git_config.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                urls = []
            if urls:
                remote = re.sub(r"^(https?://|git@)", "", urls[0]).removesuffix(".git")
                parts.append(" ".join(urls))
        subfolders: list[str] = []
        for dirpath, dirnames, _ in os.walk(path):
            depth = len(Path(dirpath).relative_to(path).parts)
            dirnames[:] = [
                name for name in sorted(dirnames)
                if name not in PRUNE_DIRS and not name.startswith(".") and depth < 3
            ]
            for name in dirnames:
                subfolders.append((Path(dirpath) / name).relative_to(path).as_posix())
            if len(subfolders) >= 400:
                break
        return " ".join(parts).lower(), subfolders, description, remote

    def _tool_list(self, args: dict[str, Any]) -> ToolResult:
        path = self.policy.readable(str(args.get("path") or "."), self.cwd)
        if not path.is_dir():
            raise ValueError(f"{self._show(path)} is not a folder.")
        folders: list[str] = []
        files: list[str] = []
        with os.scandir(path) as entries:
            for entry in entries:
                full = Path(entry.path)
                if self.policy.denied_reason(full) or self.policy.denied_reason(Path(os.path.realpath(full))):
                    continue
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                (folders if is_dir else files).append(entry.name + ("/" if is_dir else ""))
        names = sorted(folders, key=str.lower) + sorted(files, key=str.lower)
        shown = names[:LIST_ENTRIES]
        more = f"\n… and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
        body = "\n".join(shown) if shown else "(empty folder)"
        return ToolResult(
            f"{self._show(path)} ({len(folders)} folders, {len(files)} files):\n{body}{more}",
            f"Looking in {self._show(path)}",
            target=self._show(path),
        )

    def _tool_glob(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args["pattern"]).strip()
        if not pattern:
            raise ValueError("the pattern is empty")
        root = self.policy.readable(str(args.get("path") or "."), self.cwd)
        if not root.is_dir():
            raise ValueError(f"{self._show(root)} is not a folder.")
        found: list[tuple[float, str]] = []
        for full, is_dir in self._walk(root):
            rel = full.relative_to(root).as_posix()
            if glob_match(rel, pattern):
                try:
                    mtime = full.stat().st_mtime
                except OSError:
                    mtime = 0.0
                found.append((mtime, rel + ("/" if is_dir else "")))
        found.sort(key=lambda row: row[0], reverse=True)
        shown = [rel for _, rel in found[:GLOB_RESULTS]]
        more = f"\n… and {len(found) - len(shown)} more" if len(found) > len(shown) else ""
        header = f"{len(found)} match(es) for {pattern} in {self._show(root)}"
        body = ("\n" + "\n".join(shown)) if shown else ""
        return ToolResult(
            f"{header}{body}{more}{self._cut_note()}",
            f"Finding {pattern} in {self._show(root)}",
            target=f"{pattern} in {self._show(root)}",
        )

    def _tool_grep(self, args: dict[str, Any]) -> ToolResult:
        pattern = str(args["pattern"])
        if not pattern:
            raise ValueError("the pattern is empty")
        regex = re.compile(pattern)
        root = self.policy.readable(str(args.get("path") or "."), self.cwd)
        include = [item for item in expand_braces(str(args.get("include") or "").strip()) if item]
        if root.is_file():
            candidates: Iterator[tuple[Path, bool]] = iter([(root, False)])
            base = root.parent
        elif root.is_dir():
            candidates = self._walk(root)
            base = root
        else:
            raise ValueError(f"{self._show(root)} does not exist.")
        hits: list[str] = []
        files_hit = 0
        for full, is_dir in candidates:
            if is_dir or (include and not any(glob_match(full.name, item) for item in include)):
                continue
            try:
                if full.stat().st_size > GREP_FILE_BYTES or _is_binary(full):
                    continue
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = full.relative_to(base).as_posix()
            matched = False
            for number, line in enumerate(lines, start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{number}: {line.strip()[:300]}")
                    matched = True
                    if len(hits) >= GREP_MATCHES:
                        break
            files_hit += matched
            if len(hits) >= GREP_MATCHES:
                break
        where = self._show(root)
        header = f"{len(hits)} match(es) in {files_hit} file(s) under {where}"
        if len(hits) >= GREP_MATCHES:
            header += f" (first {GREP_MATCHES} shown; narrow the search)"
        body = ("\n" + "\n".join(hits)) if hits else ""
        return ToolResult(
            f"{header}{body}{self._cut_note()}",
            f"Searching {where} for “{_short(pattern, 40)}”",
            target=f"“{_short(pattern, 40)}” in {where}",
        )

    def _tool_read(self, args: dict[str, Any]) -> ToolResult:
        path = self.policy.readable(str(args["path"]), self.cwd)
        shown = self._show(path)
        if path.is_dir():
            raise ValueError(f"{shown} is a folder. Use list to see what is in it.")
        if not path.exists():
            raise ValueError(f"{shown} does not exist.")
        if path.stat().st_size > READ_MAX_BYTES:
            raise ValueError(f"{shown} is too large to read ({path.stat().st_size // 1_000_000} MB).")
        if _is_binary(path):
            raise ValueError(f"{shown} is not a text file.")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = min(READ_LINES, max(1, int(args.get("limit") or READ_LINES)))
        rows: list[str] = []
        used = 0
        last = offset - 1
        for number in range(offset, min(len(lines), offset - 1 + limit) + 1):
            row = f"{number:>6}\t{lines[number - 1][:LINE_CHARS]}"
            if used + len(row) > READ_CHARS and rows:
                break
            rows.append(row)
            used += len(row) + 1
            last = number
        self._note_read(path)
        if not lines:
            body = "(empty file)"
        elif not rows:
            body = f"(The file has {len(lines)} lines; offset {offset} is past the end.)"
        else:
            body = "\n".join(rows)
            if last < len(lines):
                body += f"\n(Lines {offset}–{last} of {len(lines)}. Read on with offset={last + 1}.)"
        return ToolResult(f"{shown}:\n{body}", f"Reading {shown}", target=shown)

    def _tool_write(self, args: dict[str, Any]) -> ToolResult:
        path = self.policy.writable(str(args["path"]), self.cwd)
        content = args["content"]
        if not isinstance(content, str):
            raise ValueError("content must be text")
        if path.is_dir():
            raise ValueError(f"{self._show(path)} is a folder.")
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._note_change(path)
        verb = "Replaced" if existed else "Created"
        return ToolResult(
            f"{verb} {self._show(path)} ({len(content.splitlines())} lines).", f"Writing {self._show(path)}",
            target=self._show(path),
        )

    def _tool_edit(self, args: dict[str, Any]) -> ToolResult:
        path = self.policy.writable(str(args["path"]), self.cwd)
        shown = self._show(path)
        old, new = args["old_string"], args["new_string"]
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old_string and new_string must be text")
        if not path.is_file():
            raise ValueError(f"{shown} does not exist. Use write to create it.")
        if not old:
            raise ValueError("old_string is empty. Use write to replace a whole file.")
        if old == new:
            raise ValueError("old_string and new_string are the same, so nothing would change.")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            raise ValueError(
                f"old_string was not found in {shown}. Read the file again and copy the exact text, "
                "including indentation."
            )
        replace_all = bool(args.get("replace_all"))
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string appears {count} times in {shown}. Include more surrounding lines so it is "
                "unique, or set replace_all."
            )
        path.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8")
        self._note_change(path)
        done = count if replace_all else 1
        return ToolResult(f"Edited {shown} ({done} replacement{'s' if done != 1 else ''}).", f"Editing {shown}",
                          target=shown)

    def _tool_bash(self, args: dict[str, Any]) -> ToolResult:
        command = str(args["command"]).strip()
        if not command:
            raise ValueError("the command is empty")
        if not sandbox_available():
            return ToolResult(
                "Commands are unavailable: this Mac has no /usr/bin/sandbox-exec, and Local Coder never runs "
                "commands without a sandbox.",
                "Commands are unavailable (no sandbox)",
                ok=False,
            )
        timeout = float(args.get("timeout_seconds") or 120)
        if timeout > 1000:
            timeout /= 1000
        timeout = min(600.0, max(1.0, timeout))
        writable: list[Path] = []
        if self.policy.workdir_real is not None:
            writable = [self.policy.workdir_real, self.policy.home_real / ".npm"]
        result = run_command(
            command,
            cwd=self.cwd,
            home=self.policy.home_real,
            writable=writable,
            tmp=self.run_tmp,
            timeout_s=timeout,
            cancel=self.cancel,
        )
        self.commands.append(command)
        if result.cancelled:
            status = "stopped by the user"
        elif result.timed_out:
            status = f"stopped after the {int(timeout)} s time limit"
        else:
            status = f"exit code {result.exit_code}"
        output = result.output.rstrip() or "(no output)"
        return ToolResult(
            f"$ {command}\n{output}\n[{status}, {result.seconds} s]",
            f"Running {_short(command, 70)}",
            ok=result.exit_code == 0,
            target=_short(command, 120),
        )

    def _tool_open_project(self, args: dict[str, Any]) -> ToolResult:
        path = self.policy.readable(str(args["path"]), self.cwd)
        shown = self._show(path)
        if not path.is_dir():
            raise ValueError(f"{shown} is not a folder.")
        if path == self.policy.home_real:
            raise ValueError("That is the whole home folder. Open the project's own folder.")
        if inside_data_root(path):
            raise ValueError(
                "That is Local Coder's own working copy of a result, not a project. Open the project's own folder."
            )
        if not looks_like_project(path):
            raise ValueError(
                f"{shown} does not look like a project (no .git, package.json, pyproject.toml, …). "
                "Open the project's own folder."
            )
        self.opened = path
        return ToolResult(
            f"Opened {shown} as the current project. Local Coder shows it to the user after your answer; "
            "their next requests apply to it.",
            f"Opening {shown}",
            target=shown,
        )
