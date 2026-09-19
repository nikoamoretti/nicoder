"""Cheap syntax checks for files the Vite preview must compile."""
from __future__ import annotations

import re
from pathlib import Path

_SMART_QUOTES = frozenset("“”‘’")
_WEB_SUFFIX = {".tsx", ".ts", ".jsx", ".js", ".css"}
_MISMATCHED_INDEX = re.compile(r"""\["[^"\n]*'\]|\['[^'\n]*"\]""")
_PAIRS = {")": "(", "]": "[", "}": "{"}


def scan_text(rel: str, text: str) -> list[str]:
    """Return problem notes for one source file."""
    problems: list[str] = []
    if any(char in text for char in _SMART_QUOTES):
        problems.append(f"{rel}: curly/smart quotes that Vite cannot parse")
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
        "Use straight quotes, not curly quotes. TypeScript generics are fine. "
        f"Problems:\n{listed}\n"
        "Reply again with complete fenced files."
    )


def _unbalanced(text: str) -> bool:
    stack: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "\"'`":
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
        if char in "([{":
            stack.append(char)
        elif char in _PAIRS:
            if not stack or stack[-1] != _PAIRS[char]:
                return True
            stack.pop()
        index += 1
    return bool(stack)


def _skip_string(text: str, start: int) -> int:
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
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
    return len(text)
