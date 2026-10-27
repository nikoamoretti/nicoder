"""Shell commands in a change request: the original project's path means the working copy, as for the file tools.

2026-10-27 benchmark (same model in Nicoder and mini-SWE-agent): the agent ran `cd <original folder> && python3 -m
unittest …`, tested the untouched original 40 times over while its edits sat in the working copy, and never finished.

Only a path the shell uses is mapped: a word that starts with the original's path (`cd <orig>`, `pytest <orig>/tests`,
`> <orig>/out.txt`) or a value after `=` or `:` (`PYTHONPATH=<orig>/src`). Text keeps the original's path: heredoc
bodies, echo and printf arguments, comments, a path inside a longer string. So a script the agent writes never carries
the private working copy's path into the user's project when they keep it. A path the copy leaves out (.git, .venv,
node_modules) stays on the original, as `read` does, and git keeps the original's path: it reads the history there,
and the sandbox refuses writes.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

# Characters that end a shell word outside quotes, and the operators among them.
_WORD_END = frozenset(" \t\n;&|()<>`")
_OPERATORS = frozenset(";&|()<>`")
_LONG_OPERATORS = ("&>>", "<<<", "&&", "||", ";;", ">>", "<<", ">&", "<&", "&>", ">|", "|&", "<>")
_REDIRECTS = frozenset({"<", ">", ">>", ">&", "<&", "&>", "&>>", ">|", "<>"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Programs whose arguments keep the original's path: echo and printf print text, git reads the original's history.
_KEEP_ORIGINAL = frozenset({"echo", "printf", "git"})
# Shells whose `-c` script is a command of its own, and searches whose pattern is text.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_SEARCHES = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})
# Search options whose value is the next word: a pattern (-e) or a pattern file (-f), else a number, glob or type.
_PATTERN_OPTIONS = frozenset({"-e", "--regexp", "-f", "--file"})
_VALUE_OPTIONS = _PATTERN_OPTIONS | {
    "-A", "-B", "-C", "-D", "-d", "-m", "-g", "-t", "-T", "-M", "-j", "-G", "--include", "--exclude",
    "--exclude-dir", "--type", "--type-not", "--glob", "--ignore", "--max-count", "--context", "--after-context",
    "--before-context",
}


def into_working_copy(command: str, *, project: Path, workdir: Path, home: Path, given: str | None = None) -> str:
    """`command` with the original project's path mapped onto `workdir` wherever the shell uses it as a path.

    `project`, `workdir` and `home` are resolved paths; `given` is the project's path as Nicoder was given it (it can
    differ, as /tmp and /private/tmp do).
    """
    return _map_command(command, _forms(project, given, home), project, workdir)


def _map_command(command: str, forms: dict[str, list[str]], project: Path, workdir: Path) -> str:
    pieces: list[str] = []
    last = 0
    program: str | None = None  # the program of the simple command being read
    redirect = text_next = script_next = pattern_seen = False
    option: str | None = None  # a search option waiting for its value
    for kind, start, end in _tokens(command):
        if kind == "op":
            op = command[start:end]
            if op in _REDIRECTS:
                redirect = True
            elif op == "<<<":
                text_next = True
            elif op != "<<":
                program, redirect, script_next, pattern_seen, option = None, False, False, False, None
            continue
        if kind == "body":
            continue
        word = command[start:end]
        mapped = word
        if text_next:
            text_next = False
        elif redirect:
            mapped = _map_word(word, forms, project, workdir)
        elif program is None:
            if not _ASSIGNMENT.match(word):
                program = os.path.basename(_unquote(word))
            mapped = _map_word(word, forms, project, workdir)
        elif program in _KEEP_ORIGINAL:
            pass
        elif program in _SHELLS:
            if script_next:
                mapped, script_next = _map_script(word, forms, project, workdir), False
            elif re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word):
                script_next = True
            else:
                mapped = _map_word(word, forms, project, workdir)
        elif program in _SEARCHES:
            if option is not None:
                if option in ("-f", "--file"):
                    mapped = _map_word(word, forms, project, workdir)
                pattern_seen, option = pattern_seen or option in _PATTERN_OPTIONS, None
            elif word.startswith("-"):
                option = word if word in _VALUE_OPTIONS else None
                pattern_seen = pattern_seen or word.startswith(("--regexp=", "--file=")) or (
                    word.startswith("-e") and len(word) > 2)
                mapped = _map_word(word, forms, project, workdir) if word.startswith("--file=") else word
            elif not pattern_seen:
                pattern_seen = True  # the pattern is text: `grep -rn "<orig>" src` looks for the original's path
            else:
                mapped = _map_word(word, forms, project, workdir)
        else:
            mapped = _map_word(word, forms, project, workdir)
        if mapped != word:
            pieces += [command[last:start], mapped]
            last = end
        redirect = False
    pieces.append(command[last:])
    return "".join(pieces)


def _map_script(word: str, forms: dict[str, list[str]], project: Path, workdir: Path) -> str:
    """The script of `bash -c '…'`, mapped as a command of its own and kept in the quotes it came in."""
    shown = str(workdir)
    if _written(shown, "") != shown or any(ch in shown for ch in "'\"$`\\"):
        return word  # a working copy path that needs escaping inside the script: left as it is
    if len(word) >= 2 and word[0] in "'\"" and _quote_end(word, 0) == len(word) - 1:
        return word[0] + _map_command(word[1:-1], forms, project, workdir) + word[0]
    return _map_word(word, forms, project, workdir)


def _forms(project: Path, given: str | None, home: Path) -> dict[str, list[str]]:
    """How the original's path can be written, longest first, by the quote it sits in ("" when unquoted)."""
    plain = {str(project)}
    if given:
        plain.add(os.path.abspath(os.path.expanduser(given)))
    variables: set[str] = set()  # $HOME expands unquoted and in double quotes; ~ only unquoted
    tilde: set[str] = set()
    if project != home and project.is_relative_to(home):
        rel = project.relative_to(home).as_posix()
        variables = {f"$HOME/{rel}", f"${{HOME}}/{rel}"}
        tilde = {f"~/{rel}"}
    unquoted = plain | variables | tilde
    unquoted |= {form.replace(" ", "\\ ") for form in unquoted}
    return {
        "": sorted(unquoted, key=len, reverse=True),
        '"': sorted(plain | variables, key=len, reverse=True),
        "'": sorted(plain, key=len, reverse=True),
    }


def _tokens(command: str) -> Iterator[tuple[str, int, int]]:
    """("word" | "op" | "body", start, end) spans of `command`: heredoc bodies are "body", comments are skipped."""
    i, n = 0, len(command)
    bodies: list[tuple[str, bool]] = []
    while i < n:
        ch = command[i]
        if ch == "\n":
            yield "op", i, i + 1
            i += 1
            for delimiter, tabs in bodies:
                end = _body_end(command, i, delimiter, tabs)
                yield "body", i, end
                i = end
            bodies.clear()
        elif ch in " \t":
            i += 1
        elif ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            newline = command.find("\n", i)
            i = n if newline < 0 else newline
        elif ch in _OPERATORS:
            op = next((long for long in _LONG_OPERATORS if command.startswith(long, i)), ch)
            yield "op", i, i + len(op)
            i += len(op)
            if op == "<<":
                i, delimiter, tabs = _delimiter(command, i)
                if delimiter:
                    bodies.append((delimiter, tabs))
        else:
            end = _word_end(command, i)
            yield "word", i, end
            i = end


def _word_end(text: str, i: int) -> int:
    while i < len(text) and text[i] not in _WORD_END:
        if text[i] == "\\":
            i += 2
        elif text[i] in "'\"":
            i = _quote_end(text, i) + 1
        else:
            i += 1
    return min(i, len(text))


def _quote_end(text: str, i: int) -> int:
    """Index of the quote that closes the one at `i` (the end of `text` when it never closes)."""
    quote, i = text[i], i + 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == "\\" and quote == '"' else 1
    return min(i, len(text))


def _delimiter(command: str, i: int) -> tuple[int, str, bool]:
    """After `<<`: where the delimiter word ends, the delimiter, and whether the body's leading tabs are dropped."""
    tabs = command.startswith("-", i)
    i += 1 if tabs else 0
    while i < len(command) and command[i] in " \t":
        i += 1
    end = _word_end(command, i)
    return end, _unquote(command[i:end]), tabs


def _body_end(command: str, start: int, delimiter: str, tabs: bool) -> int:
    """Index just past the line that closes a heredoc body starting at `start` (the end when none does)."""
    i = start
    while i < len(command):
        newline = command.find("\n", i)
        end = len(command) if newline < 0 else newline
        line = command[i:end]
        if (line.lstrip("\t") if tabs else line) == delimiter:
            return min(end + 1, len(command))
        i = end + 1
    return len(command)


def _unquote(word: str) -> str:
    return re.sub(r"\\(.)|['\"]", lambda match: match.group(1) or "", word, flags=re.S)


def _map_word(word: str, forms: dict[str, list[str]], project: Path, workdir: Path) -> str:
    """`word` with each path that starts it, or follows an `=` or `:`, mapped onto the working copy."""
    pieces: list[str] = []
    last = 0
    for start, inside in _path_starts(word):
        if start < last or start >= len(word):
            continue
        mapped = _map_path(word, start, inside, forms, project, workdir)
        if mapped is not None:
            end, text = mapped
            pieces += [word[last:start], text]
            last = end
    pieces.append(word[last:])
    return "".join(pieces)


def _path_starts(word: str) -> Iterator[tuple[int, str]]:
    """Where a path can start, with the quote it sits in: the word's start, after each `=` or `:` outside quotes,
    and after each `:` of a quoted value (`PYTHONPATH="<a>:<b>"`)."""
    yield 0, ""
    i = 0
    while i < len(word):
        ch = word[i]
        if ch == "\\":
            i += 2
        elif ch in "'\"":
            close = _quote_end(word, i)
            if i > 0 and word[i - 1] == "=":
                for k in range(i + 1, close):
                    if word[k] == ":" and (ch == "'" or word[k - 1] != "\\"):
                        yield k + 1, ch
            i = close + 1
        else:
            if ch in "=:":
                yield i + 1, ""
            i += 1


def _map_path(
    word: str, start: int, inside: str, forms: dict[str, list[str]], project: Path, workdir: Path,
) -> tuple[int, str] | None:
    """(end, replacement) when the path at `start` is in the original project, else None. `inside` is the quote
    around `start` when it is within a quoted value."""
    if not inside and word[start] in "'\"":
        quote, begin = word[start], start + 1
    else:
        quote, begin = inside, start
    end = begin
    while end < len(word) and word[end] != ":" and (word[end] != quote if quote else word[end] not in "'\""):
        end += 2 if word[end] == "\\" and quote != "'" else 1
    end = min(end, len(word))
    raw = word[begin:end]
    if _inside(_unescape(raw, quote), str(workdir)):
        return None  # already in the working copy (it can sit inside the original, as under a home-folder project)
    for form in forms[quote]:
        rest = raw[len(form):]
        if not raw.startswith(form) or (rest and not rest.startswith("/")):
            continue
        tail = _unescape(rest, quote)
        if tail and not os.path.lexists(f"{workdir}{tail}") and os.path.lexists(f"{project}{tail}"):
            return None  # the copy leaves it out (.git, .venv, node_modules): the original, as `read` does
        return end, word[start:begin] + _written(str(workdir), quote) + rest
    return None


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _unescape(text: str, quote: str) -> str:
    if quote == "'":
        return text
    if quote == '"':
        return re.sub(r'\\([\\"$`])', r"\1", text)
    return re.sub(r"\\(.)", r"\1", text, flags=re.S)


def _written(path: str, quote: str) -> str:
    """`path` as it must be written where it lands."""
    if quote == "'":
        return path.replace("'", "'\\''")
    if quote == '"':
        return re.sub(r'([\\"$`])', r"\\\1", path)
    return re.sub(r"([^A-Za-z0-9_@%+=:,./~-])", r"\\\1", path)
