"""Cheap syntax checks for files the Vite preview must compile."""
from __future__ import annotations

import re
from pathlib import Path

# A curly quote is only a problem where code needs a string delimiter: after "=", "(", "[", "{", ",", ":", "?",
# "return", "from" or "import". In text it is fine (French "l’entrepôt", JSX copy), and flagging every one sent the
# model back until it stripped all accents and quotes from a French app (Nico, 2026-10-25).
_CODE_SMART_QUOTE = re.compile(r"""(?:[=(\[{,:?]|\breturn|\bfrom|\bimport)\s*[“”‘’]""")
_WEB_SUFFIX = {".tsx", ".ts", ".jsx", ".js", ".css"}
# ["status'] in a type or an index. A name must start it, so a regex class like /["']/ is not one.
_MISMATCHED_INDEX = re.compile(r"""\["[A-Za-z_$][^"\n]*'\]|\['[A-Za-z_$][^'\n]*"\]""")


def scan_text(rel: str, text: str) -> list[str]:
    """Return problem notes for one source file."""
    problems: list[str] = []
    if _CODE_SMART_QUOTE.search(text):
        problems.append(f"{rel}: curly/smart quotes used as code quotes, which Vite cannot parse")
    if _MISMATCHED_INDEX.search(text):
        problems.append(f"{rel}: mismatched quotes in a type or index")
    # No bracket count: without a real parser it misreads apostrophes in JSX text and regex literals (5 false
    # alarms in one of Nico's projects). The project's own build, run in the checks, finds real ones.
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
