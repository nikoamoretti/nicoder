"""Structured repair state: keep the current diagnostic and its source window."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from local_coder.general.harness import format_hashed, safe_rel

_TS_HIT = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^:\n]+?\.(?:tsx|ts|jsx|js))(?:\((?P<line>\d+),(?P<col>\d+)\)|(?::(?P<line2>\d+)(?::(?P<col2>\d+))?))",
)
_VITE_BRACKET = re.compile(
    r",?-\[(?P<path>[^\]\n]+?\.(?:tsx|ts|jsx|js)):(?P<line>\d+)(?::(?P<col>\d+))?\]",
)
_PLUGIN_PATH = re.compile(
    r"\[plugin:vite:[^\]]+\]\s+(?P<path>[^:\n]+?\.(?:tsx|ts|jsx|js))",
)
_NAMED_FILE = re.compile(
    r"(?P<path>(?:src|electron|public)/[A-Za-z0-9_./-]+\.(?:tsx|ts|jsx|js))",
)
_WINDOW = 16
_IMPORT_HEAD = 12
_WEAK_ERROR = re.compile(
    r"TS6133|declared but its value is never read|is declared but never used",
    re.I,
)
_EXCERPT_BUDGET = 4500
_DIAG_BUDGET = 1800


@dataclass
class SourceHit:
    """One file/line mentioned by a compiler or preview diagnostic."""

    rel: str
    line: int | None
    column: int | None
    message: str
    sha256: str | None = None


@dataclass
class RepairState:
    """Current blocking check. Stale after the cited source identity changes."""

    goal: str
    check: str
    diagnostic: str
    hits: list[SourceHit] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)
    source_identity: dict[str, str] = field(default_factory=dict)
    changed_since: str = ""
    candidate_id: str = ""
    stale: bool = False

    def fingerprint(self) -> str:
        """Stable identity of the current diagnostic and cited files."""
        parts = [self.check, _compact(self.diagnostic)[:400]]
        for rel, digest in sorted(self.source_identity.items()):
            parts.append(f"{rel}:{digest[:16]}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def parse_diagnostic_hits(detail: str) -> list[SourceHit]:
    """Pull file/line locations out of tsc, Vite, or source-check text."""
    text = detail or ""
    hits: list[SourceHit] = []
    seen: set[tuple[str, int | None]] = set()
    for match in _VITE_BRACKET.finditer(text):
        _add_hit(hits, seen, match.group("path"), match.group("line"), match.group("col"), text)
    for match in _TS_HIT.finditer(text):
        line = match.group("line") or match.group("line2")
        col = match.group("col") or match.group("col2")
        _add_hit(hits, seen, match.group("path"), line, col, text)
    for match in _PLUGIN_PATH.finditer(text):
        _add_hit(hits, seen, match.group("path"), None, None, text)
    if not hits:
        for match in _NAMED_FILE.finditer(text):
            _add_hit(hits, seen, match.group("path"), None, None, text)
    return hits


def build_repair_state(
    goal: str,
    verification: dict[str, Any],
    root: Path,
    *,
    previous: RepairState | None = None,
) -> RepairState:
    """Build repair state from a fresh check. Rechecked source supersedes older errors."""
    health = verification.get("preview_health") if isinstance(verification.get("preview_health"), dict) else {}
    check = str(
        (health or {}).get("code")
        or verification.get("build")
        or verification.get("preview")
        or "check_failed"
    )
    diagnostic = str(verification.get("detail") or (health or {}).get("detail") or "").strip()
    hits = parse_diagnostic_hits(diagnostic)
    if not hits:
        for item in verification.get("source_problems") or []:
            hits.extend(parse_diagnostic_hits(str(item)))
    hits = _prioritize_hits(hits)
    identity: dict[str, str] = {}
    excerpts: list[str] = []
    for hit in hits:
        digest, excerpt = _excerpt_for(root, hit)
        hit.sha256 = digest
        if digest:
            identity[hit.rel] = digest
        excerpts.append(excerpt)
    changed = ""
    stale = False
    if previous is not None:
        changed = _identity_delta(previous.source_identity, identity)
        if previous.source_identity and previous.source_identity != identity:
            stale = False
            changed = changed or "cited source changed since the previous check"
    candidate_id = _tree_identity(root, list(identity))
    return RepairState(
        goal=goal.strip(),
        check=check,
        diagnostic=diagnostic,
        hits=hits,
        excerpts=excerpts,
        source_identity=identity,
        changed_since=changed,
        candidate_id=candidate_id,
        stale=stale,
    )


def refresh_identity(state: RepairState, root: Path) -> RepairState:
    """Mark the diagnostic stale when cited files no longer match the recorded hashes."""
    current: dict[str, str] = {}
    for rel in state.source_identity:
        digest = _file_sha(root / rel)
        if digest:
            current[rel] = digest
    stale = bool(state.source_identity) and current != state.source_identity
    changed = _identity_delta(state.source_identity, current) if stale else state.changed_since
    return RepairState(
        goal=state.goal,
        check=state.check,
        diagnostic=state.diagnostic,
        hits=state.hits,
        excerpts=state.excerpts if not stale else [
            f"{rel}: source changed after the check; this error is not current until rechecked"
            for rel in state.source_identity
        ],
        source_identity=state.source_identity,
        changed_since=changed,
        candidate_id=state.candidate_id,
        stale=stale,
    )


def repair_prompt_sections(state: RepairState) -> str:
    """Render the diagnostic and its source. Drops unrelated text before the error."""
    lines = [
        "Current blocking check: " + state.check,
        "Exact diagnostic:",
        _trim_diagnostic(state.diagnostic) or "(no diagnostic text)",
    ]
    if state.hits:
        loc = state.hits[0]
        where = loc.rel
        if loc.line:
            where += f":{loc.line}"
            if loc.column:
                where += f":{loc.column}"
        lines.append(f"Source location: {where}")
        if loc.sha256:
            lines.append(f"Source identity {loc.rel}: {loc.sha256}")
    if state.candidate_id:
        lines.append(f"Candidate identity: {state.candidate_id}")
    if state.changed_since:
        lines.append("Since the previous check: " + state.changed_since)
    if state.stale:
        lines.append(
            "This diagnostic is stale. The cited source changed after the check. "
            "Do not treat it as current."
        )
    if state.excerpts:
        lines.append("Relevant source (hash|line). Do not drop the failing line:")
        lines.extend(_budget_excerpts(state))
    return "\n".join(lines) + "\n"


def write_repair_state(request_root: Path, state: RepairState) -> None:
    """Persist repair state next to the request, outside the model prompt."""
    payload = asdict(state)
    (request_root / "repair_state.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8",
    )


def _add_hit(
    hits: list[SourceHit],
    seen: set[tuple[str, int | None]],
    raw_path: str,
    line: str | None,
    column: str | None,
    detail: str,
) -> None:
    rel = _project_rel(raw_path)
    if not rel:
        return
    number = int(line) if line and str(line).isdigit() else None
    key = (rel, number)
    if key in seen:
        return
    seen.add(key)
    col = int(column) if column and str(column).isdigit() else None
    snippet = detail
    name = Path(rel).name
    for raw_line in (detail or "").splitlines():
        normalized = raw_line.replace("\\", "/")
        if number is not None:
            if f"{rel}({number}" in normalized or f"{name}({number}" in normalized:
                snippet = raw_line
                break
            if f"{rel}:{number}" in normalized or f"{name}:{number}" in normalized:
                snippet = raw_line
                break
        elif rel in normalized or f"/{name}" in normalized:
            snippet = raw_line
            break
    hits.append(SourceHit(rel=rel, line=number, column=col, message=_compact(snippet)[:400]))


def _project_rel(raw: str) -> str | None:
    text = (raw or "").replace("\\", "/").strip().strip("\"'")
    if "src/" in text:
        text = "src/" + text.split("src/", 1)[1]
    text = text.lstrip("./")
    return safe_rel(text)


def _excerpt_for(root: Path, hit: SourceHit) -> tuple[str | None, str]:
    path = root / hit.rel
    if not path.is_file():
        return None, f"{hit.rel}: file is missing from the candidate"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, f"{hit.rel}: file could not be read"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunks = [f"{hit.rel} sha256={digest}"]
    if hit.line:
        start = max(1, hit.line - _WINDOW)
        chunks.append(format_hashed(text, start_line=start, limit=_WINDOW * 2 + 1, mark_line=hit.line))
        if start > 1:
            head = min(_IMPORT_HEAD, start - 1)
            chunks.append("Leading imports/types:")
            chunks.append(format_hashed(text, start_line=1, limit=head))
    else:
        chunks.append(format_hashed(text, start_line=1, limit=_WINDOW * 2))
    return digest, "\n".join(chunks)


def _file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _tree_identity(root: Path, rels: list[str]) -> str:
    parts: list[str] = []
    for rel in sorted(set(rels)):
        digest = _file_sha(root / rel)
        if digest:
            parts.append(f"{rel}:{digest[:16]}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16] if parts else ""


def _identity_delta(before: dict[str, str], after: dict[str, str]) -> str:
    notes: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old, new = before.get(rel), after.get(rel)
        if old and new and old != new:
            notes.append(f"{rel} changed")
        elif old and not new:
            notes.append(f"{rel} missing")
        elif new and not old:
            notes.append(f"{rel} added")
    return "; ".join(notes)


def _compact(text: str) -> str:
    return " ".join((text or "").split())


def _prioritize_hits(hits: list[SourceHit]) -> list[SourceHit]:
    return sorted(hits, key=lambda hit: (1 if _WEAK_ERROR.search(hit.message) else 0, hit.rel, hit.line or 0))


def _trim_diagnostic(text: str) -> str:
    raw = (text or "").strip()
    if len(raw) <= _DIAG_BUDGET:
        return raw
    lines = [line for line in raw.splitlines() if line.strip()]
    important = [line for line in lines if "error" in line.lower() and not _WEAK_ERROR.search(line)]
    weak = [line for line in lines if line not in important]
    kept: list[str] = []
    for line in important + weak:
        nxt = "\n".join(kept + [line])
        if len(nxt) > _DIAG_BUDGET:
            if not kept:
                return line[:_DIAG_BUDGET]
            break
        kept.append(line)
    return "\n".join(kept)


def _budget_excerpts(state: RepairState) -> list[str]:
    if not state.excerpts:
        return []
    if sum(len(item) for item in state.excerpts) <= _EXCERPT_BUDGET:
        return state.excerpts
    pairs = list(zip(state.hits, state.excerpts, strict=True)) if len(state.hits) == len(state.excerpts) else [
        (None, item) for item in state.excerpts
    ]
    blocking = [
        excerpt for hit, excerpt in pairs
        if hit is None or not _WEAK_ERROR.search(hit.message)
    ]
    weak = [
        excerpt for hit, excerpt in pairs
        if hit is not None and _WEAK_ERROR.search(hit.message)
    ]
    chosen = list(blocking or state.excerpts[:1])
    used = sum(len(item) for item in chosen)
    for excerpt in weak:
        if used + len(excerpt) <= _EXCERPT_BUDGET:
            chosen.append(excerpt)
            used += len(excerpt)
    if used <= _EXCERPT_BUDGET:
        return chosen
    return [_keep_marked_line(excerpt, _EXCERPT_BUDGET // max(1, len(chosen))) for excerpt in chosen]


def _keep_marked_line(excerpt: str, limit: int) -> str:
    if len(excerpt) <= limit:
        return excerpt
    mark = excerpt.find("\n>")
    if mark < 0:
        mark = excerpt.find(">")
    if mark < 0:
        return excerpt[:limit]
    start = max(0, mark - max(200, limit // 3))
    end = min(len(excerpt), start + limit)
    if ">" not in excerpt[start:end]:
        start = max(0, mark)
        end = min(len(excerpt), start + limit)
    prefix = "…\n" if start else ""
    return prefix + excerpt[start:end]
