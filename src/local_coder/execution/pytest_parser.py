"""Parse pytest machine-readable results. Stdout verdicts are never evidence.

A literal 'PASS' printed by candidate code means nothing. Zero collected
tests, missing required tests, newly skipped required tests, early process
exit, truncated channels, and corrupt reports are incomplete/invalid, never
a pass. Test identities are compared, not just failure counts.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

FRAME_RE = re.compile(r"(?m)^\s*(?:File \"?)?([A-Za-z0-9_./\\-]+\.py)\"?,? line (\d+)")
ERROR_TYPE_RE = re.compile(r"(?m)^([A-Za-z_][A-Za-z0-9_.]*?(?:Error|Exception|Failure|Timeout|Assert\w*))\b")


@dataclass
class ParsedSuite:
    """Structured suite outcome parsed from JUnit XML plus the text log."""

    collected: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    collection_error: str | None = None
    failure_test: str | None = None
    failure_type: str | None = None
    failure_message: str | None = None
    frames: list[str] = field(default_factory=list)


def _test_id(classname: str, name: str) -> str:
    base = f"{classname}::{name}" if classname else name
    return base.replace("/", ".").replace("\\", ".")


def parse_junit(xml_path: Path) -> ParsedSuite:
    """Parse pytest --junitxml output. Raises ValueError when corrupt."""
    try:
        tree = ET.parse(str(xml_path))
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"corrupt junit report: {exc}") from exc
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError("corrupt junit report: no testsuite element")
    parsed = ParsedSuite()
    for suite in suites:
        for case in suite.iter("testcase"):
            test_id = _test_id(case.get("classname", ""), case.get("name", ""))
            parsed.collected.append(test_id)
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if failure is not None:
                parsed.failed.append(test_id)
                if parsed.failure_test is None:
                    parsed.failure_test = test_id
                    parsed.failure_type = _short_type(failure.get("message", ""))
                    parsed.failure_message = (failure.get("message", "") or "")[:500]
            elif error is not None:
                parsed.errors.append(test_id)
                if parsed.failure_test is None:
                    parsed.failure_test = test_id
                    parsed.failure_type = _short_type(error.get("message", ""))
                    parsed.failure_message = (error.get("message", "") or "")[:500]
            elif skipped is not None:
                parsed.skipped.append(test_id)
            else:
                parsed.passed.append(test_id)
        for tag in ("error",):
            for node in suite.iter(tag):
                if node.get("message"):
                    parsed.collection_error = (parsed.collection_error or "") + node.get("message", "")[:500]
    parsed.collected = sorted(set(parsed.collected))
    parsed.passed = sorted(set(parsed.passed))
    parsed.failed = sorted(set(parsed.failed))
    parsed.errors = sorted(set(parsed.errors))
    parsed.skipped = sorted(set(parsed.skipped))
    return parsed


def _short_type(message: str) -> str | None:
    match = ERROR_TYPE_RE.search(message or "")
    return match.group(1) if match else None


def summarize_log_failure(log: str, failed: list[str]) -> tuple[str | None, list[str]]:
    """Extract a compact failure summary and repo-relative frames from text.

    Expected/actual values stay None unless unambiguously parseable; they
    are never invented from ambiguous logs.
    """
    frames: list[str] = []
    for match in FRAME_RE.finditer(log):
        frame = f"{match.group(1)}:{match.group(2)}"
        if frame not in frames:
            frames.append(frame)
        if len(frames) >= 8:
            break
    summary: str | None = None
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    for line in reversed(lines[-30:]):
        if line.startswith("E ") or ERROR_TYPE_RE.match(line) or "assert" in line.lower():
            summary = line[:300]
            break
    if summary is None and failed:
        summary = f"failing: {', '.join(failed[:5])}"
    return summary, [f for f in frames if not f.startswith("/")]
