"""Plain words for a step that runs a shell command: the step line says what the model is doing, not the command.

Nico, 2026-10-27, on a step line that read "ran cd /Users/…/live/can…": "is there a way to have something more
descriptive or qualitative about what the model is doing". The model says why it runs each command in bash's
`description` (542 of the 546 commands in that day's benchmark had one), and the line shows those words. For a command
without one, the words are made from the command: "Run the tests", "Install packages", "Read app.py".
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from .shell_paths import _ASSIGNMENT, _REDIRECTS, _SEARCHES, _SHELLS, _VALUE_OPTIONS, _tokens, _unquote

_LIMIT = 90
# Commands that only set up where or how the next one runs, and words that come before a command.
_SETUP = frozenset({"cd", "pushd", "popd", "export", "set", "source", ".", "unset", "ulimit", "trap", "true", "clear",
                    "for", "while", "until", "if", "case", "esac", "fi", "done", "function"})
_PREFIXES = frozenset({"do", "then", "else", "time", "env", "nice", "nohup", "exec", "sudo", "!", "{"})
# Commands that say little next to the one they go with (`sleep 2 && curl …`, `echo --- && cat …`).
_WEAK = frozenset({"sleep", "echo", "printf", "pwd"})
# `uv run X`, `bundle exec X` …: the command is X (as for `python -m X` and `npx X`).
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "bundle": "exec", "pnpm": "exec", "yarn": "dlx"}
_TESTS = frozenset({"pytest", "py.test", "unittest", "jest", "vitest", "mocha", "tox", "nox", "rspec", "phpunit"})
_PACKAGES = frozenset({"npm", "yarn", "pnpm", "bun"})
_INSTALLERS = frozenset({"pip", "pip3", "uv", "poetry", "pipenv", "brew", "gem", "bundle", "composer", "apt-get"})
_TOOLCHAINS = frozenset({"go", "cargo", "swift", "dotnet", "deno", "flutter", "make", "xcodebuild", "gradle", "mvn"})
_BUILDS = frozenset({"tsc", "webpack", "vite", "esbuild", "rollup", "cmake", "ninja"})
_CHECKS = frozenset({"ruff", "flake8", "pylint", "mypy", "pyright", "eslint", "swiftlint", "shellcheck"})
_FORMATS = frozenset({"black", "isort", "prettier", "gofmt", "rustfmt", "swiftformat"})
_LANGUAGES = {"python": "Python", "node": "JavaScript", "ruby": "Ruby", "perl": "Perl", "php": "PHP",
              "bash": "shell", "sh": "shell", "zsh": "shell", "osascript": "AppleScript"}
_READS = frozenset({"cat", "head", "tail", "less", "more", "nl", "wc", "bat", "xxd", "strings", "sed", "awk", "jq"})
_LISTS = frozenset({"ls", "tree", "du", "eza"})
_FINDS = frozenset({"find", "fd", "mdfind"})
_FETCHES = frozenset({"curl", "wget", "http", "xh"})
_FILES = {"mkdir": "Make the folder", "rm": "Delete", "rmdir": "Delete", "mv": "Move", "cp": "Copy", "touch": "Create"}
_SAYS = {
    "echo": "Print a message", "printf": "Print a message", "pwd": "Check the current folder", "sleep": "Wait",
    "kill": "Stop a process", "pkill": "Stop a process", "killall": "Stop a process", "ps": "Check running processes",
    "lsof": "Check running processes", "which": "Check which tools are installed",
    "type": "Check which tools are installed", "command": "Check which tools are installed", "diff": "Compare files",
    "sqlite3": "Query the database", "psql": "Query the database", "mysql": "Query the database",
}


def describe(command: str, description: object = None) -> str:
    """The step line for a command: the model's own words for it (bash's `description`), else words made from it."""
    words = " ".join(str(description or "").split()).rstrip(".")
    return _clip(words or from_command(command), _LIMIT)


def from_command(command: str) -> str:
    """Words for a command the model gave none for: "Run the tests", "Install packages", "Read app.py"."""
    weak = ""
    for words, writes in _commands(command):
        words = _unwrap(words)
        if not words or words[0] in _SETUP:
            continue
        if words[0] in _WEAK and not writes:
            weak = weak or _words(words, writes)
            continue
        return _words(words, writes)
    return weak or "Set up the shell"


def _commands(command: str) -> Iterator[tuple[list[str], str]]:
    """Each simple command's words (unquoted, redirects left out) and the file it writes with > or >>."""
    words: list[str] = []
    writes = redirect = ""
    end = -1
    for kind, start, stop in _tokens(command):
        text = command[start:stop]
        if kind == "body":
            continue
        if kind == "op" and (text in _REDIRECTS or text == "<<<"):
            if words and words[-1].isdigit() and end == start:  # the 2 of 2>&1
                words.pop()
            redirect = text
        elif kind == "op":
            if words:
                yield words, writes
            words, writes, redirect = [], "", ""
        elif redirect:
            if redirect in {">", ">>", "&>", "&>>", ">|"} and not text.startswith("/dev/"):
                writes = _unquote(text)
            redirect = ""
        else:
            words.append(_unquote(text))
        end = stop
    if words:
        yield words, writes


def _unwrap(words: list[str]) -> list[str]:
    """The command itself, without assignments, prefixes (time, timeout 60) or a runner (python -m, npx, uv run)."""
    while words:
        head = os.path.basename(words[0])
        if head in _PREFIXES or _ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif head in {"timeout", "gtimeout"}:
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", words[0])):
                words = words[1:]
        elif head.startswith("python") and words[1:2] == ["-m"]:
            words = words[2:]
        elif head in {"npx", "bunx", "pnpx"}:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        elif len(words) > 1 and _RUNNERS.get(head) == words[1]:
            words = words[2:]
        else:
            break
    return words


def _words(words: list[str], writes: str) -> str:
    name, args = os.path.basename(words[0]), words[1:]
    rest = _operands(args)
    sub = rest[0] if rest else ""
    script = rest[1] if name in _PACKAGES and sub == "run" and len(rest) > 1 else sub
    language = _LANGUAGES.get(re.sub(r"[\d.]+$", "", name))
    if name in {"cat", "echo", "printf", "tee"} and (writes or (name == "tee" and rest)):
        return f"Write {_name(writes or rest[-1])}"
    script_flag = next((i for i, word in enumerate(args) if re.fullmatch(r"-[a-z]*c[a-z]*", word)), None)
    if name in _SHELLS and script_flag is not None and script_flag + 1 < len(args):
        return from_command(args[script_flag + 1])
    if name in _TESTS or (name in _PACKAGES | _TOOLCHAINS and (script.startswith("test") or "test" in rest)):
        return "Run the tests"
    if name in _PACKAGES:
        if sub in {"install", "i", "ci", "add"} or (name == "yarn" and not sub):
            return "Install packages"
        if script in {"build", "compile"}:
            return "Build the project"
        if script in {"dev", "start", "serve", "preview"}:
            return "Start the app"
        return f"Run the {script} script" if sub == "run" and len(rest) > 1 else f"Run {name} {sub}".strip()
    if name in _INSTALLERS and {"install", "add", "sync", "require"} & set(rest[:2]):
        return "Install packages"
    if name in _TOOLCHAINS:
        if sub in {"get", "add", "install", "mod", "fetch"}:
            return "Install packages"
        if sub == "run":
            return "Run the app"
        if sub in {"", "build", "all", "package", "compile", "assemble"}:
            return "Build the project"
    if name in _BUILDS:
        return "Check the types" if name == "tsc" and "--noEmit" in args else "Build the project"
    if name in _CHECKS:
        return "Format the code" if sub == "format" else "Check the code for problems"
    if name in _FORMATS:
        return "Check the formatting" if "--check" in args else "Format the code"
    if args[:1] and args[0] in {"--version", "-v", "-V", "version"}:
        return f"Check the {name} version"
    if language:
        if {"-c", "-e", "--eval", "-"} & set(args) or not rest:
            return f"Try some {language}"
        return f"Run a {language} script" if rest[0].startswith("$") else f"Run {_name(rest[0])}"
    if name == "sed" and any(word.startswith("-i") for word in args):
        return f"Edit {_name(rest[-1])}" if rest else "Edit a file"
    if name in _READS:
        plain = _operands(args, takes_value=frozenset())  # tail -f follows a file; -n 40 leaves a number
        plain = plain[1:] if name in {"sed", "awk", "jq"} else plain  # their first word is a script
        files = [word for word in plain if not re.fullmatch(r"[+-]?\d+", word)]
        return f"Read {_some(files)}" if files else "Read the output"
    if name in _LISTS:
        return f"List the files in {_name(sub)}" if sub else "List the files"
    if name in _FINDS:
        named = next((args[i + 1] for i, word in enumerate(args[:-1]) if word in {"-name", "-iname"}), "")
        return f"Look for files named {named}" if named else "Look for files"
    if name == "git":
        rest = _operands(args[2:] if args[:1] == ["-C"] else args)
        sub = rest[0] if rest else ""
        if sub == "grep":
            return _search(args, rest[1:2])
        if sub in {"status", "diff"}:
            return "Look at what changed"
        if sub in {"log", "show", "blame", "shortlog", "reflog"}:
            return "Read the git history"
        return f"Run git {sub}".strip()
    if name in _SEARCHES:
        return _search(args, rest[:1])
    if name in _FETCHES:
        url = next((word for word in rest if "://" in word or word.startswith("localhost")), "")
        host = urlsplit(url if "://" in url else f"http://{url}").netloc.rsplit("@", 1)[-1] if url else ""
        return f"Fetch {host}" if host else "Fetch a web page"
    if name in _FILES and rest:
        return f"{_FILES[name]} {_some(rest[:1] if name in {'mv', 'cp'} else rest)}"
    if name in _SAYS:
        return _SAYS[name]
    if name == "open" and sub:
        return f"Open {_name(sub)}"
    if "/" in words[0]:
        return f"Run {_name(words[0])}"
    return f"Run {name} {sub}" if re.fullmatch(r"[a-z][\w:-]*", sub) else f"Run {name}"


def _operands(args: list[str], takes_value: frozenset[str] = _VALUE_OPTIONS) -> list[str]:
    """The words that are not options, nor the value of a common option that takes one (-A 3, --include '*.py')."""
    operands: list[str] = []
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in takes_value
        else:
            operands.append(word)
    return operands


def _search(args: list[str], operands: list[str]) -> str:
    given = next((args[i + 1] for i, word in enumerate(args[:-1]) if word in {"-e", "--regexp"}), "")
    pattern = given or (operands[0] if operands else "")
    return f"Search for “{_clip(pattern, 40)}”" if pattern else "Search the files"


def _name(path: str) -> str:
    """How a path reads in a step: its last part ("src/app.py" is "app.py"), and "." is the project."""
    if path in {"", ".", "./"}:
        return "the project"
    return os.path.basename(path.rstrip("/")) or path


def _some(paths: list[str]) -> str:
    more = f" and {len(paths) - 1} more" if len(paths) > 1 else ""
    return f"{_name(paths[0])}{more}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
