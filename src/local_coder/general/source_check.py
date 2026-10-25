"""Cheap syntax checks for files the Vite preview must compile."""
from __future__ import annotations

import re
from pathlib import Path

# A curly quote is only a problem where code needs a string delimiter: after "=", "(", "[", "{", ",", ":", "?",
# "return", "from" or "import". In text it is fine (French "l’entrepôt", JSX copy), and flagging every one sent the
# model back until it stripped all accents and quotes from a French app (Nico, 2026-10-25).
# Not after "[": a regex class like /[’']/g holds curly quotes on purpose.
_CODE_SMART_QUOTE = re.compile(r"""(?:[=({,:?]|\breturn|\bfrom|\bimport)\s*[“”‘’]""")
_WEB_SUFFIX = {".tsx", ".ts", ".jsx", ".js", ".css"}
_PAIRS = {")": "(", "]": "[", "}": "{"}
# ["status'] in a type or an index. A name must start it, so a regex class like /["']/ is not one.
_MISMATCHED_INDEX = re.compile(r"""\["[A-Za-z_$][^"\n]*'\]|\['[A-Za-z_$][^'\n]*"\]""")


def scan_text(rel: str, text: str) -> list[str]:
    """Return problem notes for one source file."""
    problems: list[str] = []
    if _CODE_SMART_QUOTE.search(text):
        problems.append(f"{rel}: curly/smart quotes used as code quotes, which Vite cannot parse")
    if _MISMATCHED_INDEX.search(text):
        problems.append(f"{rel}: mismatched quotes in a type or index")
    if _unbalanced(text):
        problems.append(f"{rel}: unmatched brackets or braces")
    return problems


def scan_changed_sources(root: Path, rels: list[str]) -> list[str]:
    """Scan written web sources. Missing files are skipped."""
    problems: list[str] = []
    for rel in rels:
        path = root / rel
        if path.suffix.lower() not in _WEB_SUFFIX or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        problems.extend(scan_text(rel, text))
    return problems


def parse_retry_message(problems: list[str]) -> str:
    """Ask the model to rewrite files Vite cannot compile."""
    listed = "\n".join(f"- {item}" for item in problems)
    return (
        "The files you wrote do not parse, so the preview cannot run. "
        "Use straight quotes around code strings; curly quotes inside text are fine. TypeScript generics are fine. "
        f"Problems:\n{listed}\n"
        "Reply again with write or edit blocks."
    )


def _unbalanced(text: str) -> bool:
    """True when brackets or braces outside strings and comments do not pair up.

    A quote right after a letter or digit is an apostrophe in text ("it's", "l'entrepôt"), not the start of a
    string, and a '…' or "…" string that never closes on its line is text too. Without those two rules this
    count raised "unmatched brackets" for five good files of a French app (Nico, 2026-10-25).
    """
    stack: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "\"'`" and not (index and text[index - 1].isalnum()):
            index = _skip_string(text, index)
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char == "/" and _regex_can_start(text, index):
            index = _skip_regex(text, index)
            continue
        if char in "([{":
            stack.append(char)
        elif char in _PAIRS:
            if not stack or stack[-1] != _PAIRS[char]:
                return True
            stack.pop()
        index += 1
    return bool(stack)


def _skip_string(text: str, start: int) -> int:
    """The index after the string that opens at `start`; just past the quote when it never closes on its line."""
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        if text[index] == "\n" and quote != "`":
            return start + 1
        if quote == "`" and text[index] == "$" and index + 1 < len(text) and text[index + 1] == "{":
            # Treat interpolated templates as opaque; resume after the matching brace.
            depth = 1
            index += 2
            while index < len(text) and depth:
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                index += 1
            continue
        index += 1
    return start + 1 if quote != "`" else len(text)


_REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = ("return", "typeof", "case", "in", "of", "do", "else", "throw", "yield", "await")


def _regex_can_start(text: str, index: int) -> bool:
    """Whether a "/" here opens a regex literal (after an operator or a keyword) rather than dividing.

    Never in JSX: "/>" closes a tag and "</" opens a closing one (a self-closing <Row ... /> after a {prop}
    was read as a regex and raised "unmatched brackets" for good React code).
    """
    if index + 1 < len(text) and text[index + 1] == ">":
        return False
    if index and text[index - 1] == "<":
        return False
    before = text[:index].rstrip()
    if not before:
        return True
    if before[-1] in _REGEX_AFTER:
        return True
    word = before.split()[-1] if before.split() else ""
    return word in _REGEX_KEYWORDS


def _skip_regex(text: str, start: int) -> int:
    """The index after a regex literal and its flags; just past the "/" when it does not close on its line."""
    index = start + 1
    in_class = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return start + 1
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "/":
            index += 1
            while index < len(text) and text[index].isalpha():
                index += 1
            return index
        index += 1
    return start + 1
