"""Structured repair state: one active diagnostic, current-request source visibility.

Historical reads, current-request inclusion, current omissions, and post-edit
invalidation are tracked separately. A line marked available must appear in
this request. Previously displayed source is not current visibility.

Active-target selection is deterministic: group diagnostics that share a file
and line, then pick the group with the lowest severity. Syntax and missing
imports outrank type errors; unused-name errors are last. A one-error repair
is an internal step; the original request and remaining diagnostics stay in
runtime state.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from local_coder.general.harness import contained_file, format_lines, safe_rel

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
_WEAK_ERROR = re.compile(
    r"TS6133|TS6192|declared but its value is never read|is declared but never used"
    r"|All imports in import declaration are unused",
    re.I,
)
_TSC_LINE = re.compile(r"\(\d+,\d+\): error TS\d+")
_TSC_ITEM = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^:\n]+?\.(?:tsx|ts|jsx|js))\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"error\s+(?P<code>TS\d+):\s*(?P<msg>.*)$"
)
_LOCATION = re.compile(r"\(\d+,\d+\)|:\d+:\d+")
_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b"
    r"|^\s*(?:export\s+)?(?:const|let)\s+\w+\s*(?::[^=]+)?=\s*(?:async\s*)?"
    r"(?:\([^)]*\)\s*(?::[^=]+)?=>|function\b|use(?:Callback|Memo)\b)"
    r"|^\s*(?:export\s+)?(?:interface|type|class|enum)\s+\w+"
)
_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:type|interface|class|enum|function|const|let)\s+(?P<name>\w+)"
    r"|^\s*export\s+default\b"
)
_OPEN_TAG = re.compile(r"<([A-Za-z][\w.]*)\b")
_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_QUOTED = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
_ERROR_RADIUS = 3
_READ_CHUNK = 100
_HEAD_MAX = 12
_NAMED_LINES = 24
_DIAG_BUDGET = 2400
_SCOPE_CAP = 80
_SMALL_FILE = 80
_REMAINING_BUDGET = 1600
_JS_KW = frozenset(
    {
        "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete",
        "do", "else", "enum", "export", "extends", "false", "finally", "for", "from", "function",
        "if", "import", "in", "instanceof", "let", "new", "null", "return", "super", "switch",
        "this", "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield", "async",
        "await", "of", "as", "type", "interface", "undefined",
    }
)


@dataclass
class SourceHit:
    """One file/line mentioned by a compiler or preview diagnostic."""

    rel: str
    line: int | None
    column: int | None
    message: str
    sha256: str | None = None
    code: str = ""


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
    active: list[SourceHit] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Stable identity of the current diagnostic and cited files."""
        parts = [self.check, _compact(self.diagnostic)[:400]]
        for rel, digest in sorted(self.source_identity.items()):
            parts.append(f"{rel}:{digest[:16]}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class FileView:
    """Historical read progress for one content identity. Not current-request visibility."""

    sha256: str
    requested: set[int] = field(default_factory=set)
    shown: set[int] = field(default_factory=set)


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


def parse_tsc_items(detail: str) -> list[SourceHit]:
    """One hit per tsc error line, including several codes on the same file line."""
    items: list[SourceHit] = []
    seen: set[tuple[str, int | None, str]] = set()
    for raw in diagnostic_lines(detail):
        match = _TSC_ITEM.search(raw.strip())
        if not match:
            continue
        rel = _project_rel(match.group("path"))
        if not rel:
            continue
        line = int(match.group("line"))
        code = match.group("code")
        key = (rel, line, code)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            SourceHit(
                rel=rel,
                line=line,
                column=int(match.group("col")),
                message=_compact(raw)[:400],
                code=code,
            )
        )
    return items


def select_active(hits: list[SourceHit], diagnostic: str = "") -> list[SourceHit]:
    """Pick one diagnostic group: same file and line, lowest severity, then path and line.

    Severity, lowest first:
    0 syntax/parse (TS10xx/TS11xx, unmatched, unexpected token)
    1 missing module / no exported member / cannot resolve import
    2 illegal assignment (TS2779)
    3 cannot find name (TS2304)
    4 type-only import (TS1484)
    5 other type errors
    6 unused names (TS6133/TS6192)
    """
    pool = parse_tsc_items(diagnostic) if diagnostic else []
    pool = pool or list(hits)
    if not pool:
        return []
    groups: dict[tuple[str, int], list[SourceHit]] = {}
    for hit in pool:
        groups.setdefault((hit.rel, hit.line or 0), []).append(hit)
    ranked = sorted(
        groups.values(),
        key=lambda group: (min(_severity(item) for item in group), group[0].rel, group[0].line or 0),
    )
    return ranked[0]


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
    active = select_active(hits, diagnostic)
    identity: dict[str, str] = {}
    for hit in hits + active:
        digest = _file_sha(root / hit.rel) if contained_file(root, hit.rel) else None
        hit.sha256 = digest
        if digest:
            identity[hit.rel] = digest
    changed = ""
    if previous is not None and previous.source_identity and previous.source_identity != identity:
        changed = _identity_delta(previous.source_identity, identity) or "cited source changed since the previous check"
    state = RepairState(
        goal=goal.strip(),
        check=check,
        diagnostic=diagnostic,
        hits=hits,
        active=active,
        source_identity=identity,
        changed_since=changed,
        candidate_id=_tree_identity(root, list(identity)),
        stale=False,
    )
    rendered, _shown = render_source(state, root, None, budget=12_000)
    state.excerpts = [rendered] if rendered else []
    return state


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
        active=state.active,
        excerpts=state.excerpts if not stale else [
            f"{rel}: source changed after the check; this error is not current until rechecked"
            for rel in state.source_identity
        ],
        source_identity=state.source_identity,
        changed_since=changed,
        candidate_id=state.candidate_id,
        stale=stale,
    )


def error_keys(state: RepairState) -> frozenset[str]:
    """Diagnostic identity without line numbers, so shifted lines are not new errors."""
    keys = {_LOCATION.sub("", _compact(line)) for line in diagnostic_lines(state.diagnostic)}
    return frozenset(keys or {state.check})


def diagnostic_lines(detail: str) -> list[str]:
    """Error lines of a diagnostic. npm banners and truncated headers are dropped for tsc output."""
    lines = [line.rstrip() for line in (detail or "").splitlines() if line.strip()]
    if not any(_TSC_LINE.search(line) for line in lines):
        return lines
    kept: list[str] = []
    continuation = False
    for line in lines:
        if _TSC_LINE.search(line):
            kept.append(line)
            continuation = True
        elif continuation and line.startswith((" ", "\t")):
            kept.append(line)
        else:
            continuation = False
    return kept


def diagnostic_text(state: RepairState, budget: int = _DIAG_BUDGET) -> str:
    """Exact diagnostic lines within budget. Unused-name errors are dropped before real ones."""
    lines = diagnostic_lines(state.diagnostic)
    text = "\n".join(lines)
    if len(text) <= budget:
        return text
    order = [i for i, line in enumerate(lines) if not _WEAK_ERROR.search(line)]
    order += [i for i, line in enumerate(lines) if _WEAK_ERROR.search(line)]
    kept: set[int] = set()
    used = 0
    for index in order:
        cost = len(lines[index]) + 1
        if used + cost > budget and kept:
            continue
        kept.add(index)
        used += cost
    dropped = len(lines) - len(kept)
    body = "\n".join(lines[i] for i in sorted(kept))
    if dropped:
        body += f"\n({dropped} more error lines not shown)"
    return body


def active_diagnostic_text(state: RepairState) -> str:
    """Exact text of the active group. Empty when the check has no located error."""
    active = state.active or select_active(state.hits, state.diagnostic)
    if not active:
        return ""
    keys = {(hit.rel, hit.line) for hit in active}
    codes = {hit.code for hit in active if hit.code}
    kept: list[str] = []
    for line in diagnostic_lines(state.diagnostic):
        item = _TSC_ITEM.search(line.strip())
        if item:
            rel = _project_rel(item.group("path"))
            number = int(item.group("line"))
            if (rel, number) in keys:
                kept.append(line)
            continue
        for hit in active:
            if hit.rel in line and (hit.line is None or str(hit.line) in line):
                if not codes or not hit.code or hit.code in line:
                    kept.append(line)
                    break
    if kept:
        return "\n".join(kept)
    return "\n".join(hit.message for hit in active)


def remaining_diagnostic_text(state: RepairState, budget: int = _REMAINING_BUDGET) -> str:
    """Unresolved diagnostics that are not the active target."""
    active = state.active or select_active(state.hits, state.diagnostic)
    keys = {(hit.rel, hit.line) for hit in active}
    rest: list[str] = []
    for line in diagnostic_lines(state.diagnostic):
        item = _TSC_ITEM.search(line.strip())
        if item:
            rel = _project_rel(item.group("path"))
            number = int(item.group("line"))
            if (rel, number) in keys:
                continue
        elif any(hit.rel in line and (hit.line is None or str(hit.line) in line) for hit in active):
            continue
        rest.append(line)
    if not rest:
        return ""
    ranked = [line for line in rest if not _WEAK_ERROR.search(line)]
    ranked += [line for line in rest if _WEAK_ERROR.search(line)]
    text = "\n".join(ranked)
    if len(text) <= budget:
        return text
    kept: list[str] = []
    used = 0
    for line in ranked:
        cost = len(line) + 1
        if used + cost > budget and kept:
            break
        kept.append(line)
        used += cost
    dropped = len(ranked) - len(kept)
    body = "\n".join(kept)
    if dropped:
        body += f"\n({dropped} more remaining error lines not shown)"
    return body


def error_lines(state: RepairState) -> dict[str, list[int]]:
    """Cited lines of the active target. Files cited without a line map to []."""
    marks: dict[str, list[int]] = {}
    for hit in state.active or state.hits:
        lines = marks.setdefault(hit.rel, [])
        if hit.line and hit.line not in lines:
            lines.append(hit.line)
    return marks


def serve_read(views: dict[str, FileView], root: Path, rel: str) -> tuple[str, int]:
    """Queue the next bounded chunk of a candidate file. Returns (status, new line count)."""
    path = contained_file(root, rel)
    if path is None:
        return "refused", 0
    if not path.is_file():
        return "missing", 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unreadable", 0
    digest = _sha(text)
    view = views.get(rel)
    if view is None or view.sha256 != digest:
        view = FileView(sha256=digest)
        views[rel] = view
    total = len(text.splitlines())
    unseen = [number for number in range(1, total + 1) if number not in view.shown]
    if not unseen:
        return "seen", 0
    chunk = set(unseen[:_READ_CHUNK])
    view.requested |= chunk
    return "read", len(chunk)


def note_shown(views: dict[str, FileView], root: Path, shown: dict[str, set[int]]) -> None:
    """Record which lines actually reached the prompt, so the next read continues after them."""
    for rel, lines in shown.items():
        if not lines:
            continue
        digest = _file_sha(root / rel)
        if not digest:
            continue
        view = views.get(rel)
        if view is None or view.sha256 != digest:
            view = FileView(sha256=digest)
            views[rel] = view
        view.shown |= lines


def invalidate_views(views: dict[str, FileView], rels: list[str]) -> None:
    """Drop historical views for files an edit changed. Recheck before the next target."""
    for rel in rels:
        views.pop(rel, None)


def format_line_ranges(lines: set[int]) -> str:
    """Compact 1-based ranges for a visibility manifest."""
    if not lines:
        return "(none)"
    ordered = sorted(lines)
    parts: list[str] = []
    start = prev = ordered[0]
    for number in ordered[1:]:
        if number == prev + 1:
            prev = number
            continue
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = number
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(parts)


def render_source(
    state: RepairState,
    root: Path,
    views: dict[str, FileView] | None,
    *,
    budget: int,
) -> tuple[str, dict[str, set[int]]]:
    """Active-target source plus extra reads that fit. Packet lines are the last thing dropped."""
    views = views or {}
    texts, notes, missing = _load_texts(state, root, views)
    marks = error_lines(state)
    packet = _packet_lines(state, texts, root)
    extras = _fresh_reads(views, texts, packet)
    order = list(packet) + [rel for rel in extras if rel not in packet]
    selection: dict[str, set[int]] = {rel: set() for rel in texts}

    def render() -> str:
        return _format_body(order, texts, notes, missing, selection, marks, views)

    def take(rel: str, lines: set[int], *, mandatory: bool) -> None:
        if rel not in selection or not lines:
            return
        before = set(selection[rel])
        selection[rel] = before | lines
        if mandatory:
            return
        if len(render()) > budget:
            selection[rel] = before

    for rel, lines in packet.items():
        lock = set()
        source = texts.get(rel, "").splitlines()
        total = len(source)
        for mark in marks.get(rel, []):
            lock |= _statement_span(source, mark) if source else {mark}
            lock |= _window(mark, 0, total)
        take(rel, lock or set(lines), mandatory=True)
        take(rel, lines - selection.get(rel, set()), mandatory=False)
    extra_group = [(rel, extras[rel]) for rel in extras if extras[rel]]
    if extra_group:
        _fill_round_robin(extra_group, selection, render, budget)
    shown = {rel: lines for rel, lines in selection.items() if lines}
    return render(), shown


def repair_prompt_sections(
    state: RepairState,
    root: Path | None = None,
    views: dict[str, FileView] | None = None,
    *,
    budget: int = 9000,
    status: str = "",
) -> str:
    """Render the check, status, diagnostic, and source for a repair prompt."""
    text, _shown = render_repair_sections(state, root, views, budget=budget, status=status)
    return text


def render_repair_sections(
    state: RepairState,
    root: Path | None,
    views: dict[str, FileView] | None,
    *,
    budget: int,
    status: str = "",
) -> tuple[str, dict[str, set[int]]]:
    """Header, active diagnostic, remaining errors, visibility, and source for this request."""
    views = views or {}
    active = state.active or select_active(state.hits, state.diagnostic)
    remaining = remaining_diagnostic_text(state)
    total = max(len(diagnostic_lines(state.diagnostic)), 1)
    head = [f"Current blocking check: {state.check}"]
    if status:
        head.append(status)
    head.append(
        f"Active repair target (fix this target only; 1 of {total} remaining). "
        "The original user request is still the product goal."
    )
    head.append("Exact diagnostic:")
    exact = active_diagnostic_text(state) or diagnostic_text(state, min(_DIAG_BUDGET, max(600, budget // 3)))
    head.append(exact or "(no diagnostic text)")
    if remaining:
        head.append("Remaining diagnostics (kept in runtime; not this turn):")
        head.append(remaining)
    if state.goal:
        head.append("Behavior that must be preserved: " + _compact(state.goal)[:400])
    if active:
        loc = active[0]
        where = loc.rel
        if loc.line:
            where += f":{loc.line}"
            if loc.column:
                where += f":{loc.column}"
        head.append(f"Active source location: {where}")
    if state.candidate_id:
        head.append(f"Candidate identity: {state.candidate_id}")
    if state.changed_since:
        head.append("Since the previous check: " + state.changed_since)
    if state.stale:
        head.append(
            "This diagnostic is stale. The cited source changed after the check. Do not treat it as current."
        )
    head.append("Source (hash|line; > marks an error line; … marks lines not shown):")
    prefix = "\n".join(head) + "\n"
    shown: dict[str, set[int]] = {}
    if root is None:
        body = "\n".join(state.excerpts)
        visibility = ""
    else:
        body, shown = render_source(state, root, views, budget=max(800, budget - len(prefix) - 800))
        texts, _notes, _missing = _load_texts(state, root, views)
        visibility = _visibility(state, shown, views, texts) + "\n"
    return prefix + visibility + body + "\n", shown


def write_repair_state(request_root: Path, state: RepairState) -> None:
    """Persist repair state next to the request, outside the model prompt."""
    payload = asdict(state)
    (request_root / "repair_state.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8",
    )


def _visibility(
    state: RepairState,
    shown: dict[str, set[int]],
    views: dict[str, FileView],
    texts: dict[str, str],
) -> str:
    rows = [
        "This request includes:",
    ]
    if shown:
        for rel in shown:
            total = len(texts[rel].splitlines()) if rel in texts else max(shown[rel])
            digest = _sha(texts[rel])[:12] if rel in texts else "unknown"
            rows.append(f"- {rel} lines {format_line_ranges(shown[rel])} of {total} (sha256 {digest})")
    else:
        rows.append("- (no source lines in this request)")
    omitted_rels: list[str] = []
    seen_names: set[str] = set(shown)
    for hit in state.hits:
        if hit.rel not in seen_names:
            omitted_rels.append(hit.rel)
            seen_names.add(hit.rel)
    for rel, view in views.items():
        if rel not in seen_names and (view.shown or view.requested):
            omitted_rels.append(rel)
            seen_names.add(rel)
    rows.append("This request does not include:")
    if omitted_rels:
        for rel in omitted_rels:
            previous = views.get(rel)
            if previous and previous.shown:
                rows.append(f"- {rel} (read earlier; not in this request)")
            else:
                rows.append(f"- {rel} (not in this request)")
    else:
        rows.append("- no other cited source")
    rows.append(
        "Large files are shown in bounded portions, not as complete files. "
        "Previously displayed source is not available unless listed under 'This request includes'."
    )
    return "\n".join(rows)


def _load_texts(
    state: RepairState,
    root: Path,
    views: dict[str, FileView],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    rels: list[str] = []
    for hit in (state.active or state.hits) + state.hits:
        if hit.rel not in rels:
            rels.append(hit.rel)
    texts: dict[str, str] = {}
    notes: dict[str, str] = {}
    missing: list[str] = []
    for rel in list(rels):
        _try_load(root, rel, state, texts, notes, missing)
        if rel in texts:
            for _names, spec in _related_imports(_active_hit(state, rel), texts[rel].splitlines()):
                dep = _resolve_spec(root, rel, spec)
                if dep and dep not in rels:
                    rels.append(dep)
                    _try_load(root, dep, state, texts, notes, missing)
    for rel in views:
        if rel not in texts and rel not in notes:
            _try_load(root, rel, state, texts, notes, missing)
    return texts, notes, missing


def _try_load(
    root: Path,
    rel: str,
    state: RepairState,
    texts: dict[str, str],
    notes: dict[str, str],
    missing: list[str],
) -> None:
    path = contained_file(root, rel)
    if path is None:
        notes[rel] = f"{rel}: refused, the path resolves outside the candidate"
        return
    if not path.is_file():
        missing.append(rel)
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        notes[rel] = f"{rel}: file could not be read"
        return
    recorded = state.source_identity.get(rel)
    if state.stale and recorded and recorded != _sha(text):
        notes[rel] = f"{rel}: source changed after the check; this error is not current until rechecked"
        return
    texts[rel] = text


def _active_hit(state: RepairState, rel: str) -> SourceHit:
    for hit in state.active or state.hits:
        if hit.rel == rel:
            return hit
    return SourceHit(rel=rel, line=None, column=None, message="")


def _packet_lines(state: RepairState, texts: dict[str, str], root: Path) -> dict[str, set[int]]:
    chosen: dict[str, set[int]] = {}
    for hit in state.active or state.hits[:1]:
        text = texts.get(hit.rel)
        if not text:
            continue
        lines = text.splitlines()
        total = len(lines)
        bucket = chosen.setdefault(hit.rel, set())
        bucket |= set(_head(lines))
        mark = hit.line if hit.line and 1 <= hit.line <= total else None
        if mark:
            bucket |= _statement_span(lines, mark)
            bucket |= _jsx_span(lines, mark)
            bucket |= _enclosing_span(lines, mark)
            bucket |= _window(mark, _ERROR_RADIUS, total)
        else:
            bucket |= set(range(1, min(total, _NAMED_LINES) + 1))
        for names, spec in _related_imports(hit, lines):
            dep = _resolve_spec(root, hit.rel, spec)
            if not dep or dep not in texts:
                continue
            dep_lines = texts[dep].splitlines()
            chosen.setdefault(dep, set())
            chosen[dep] |= _export_span(dep_lines, names)
    return {rel: lines for rel, lines in chosen.items() if lines}


def _fresh_reads(
    views: dict[str, FileView],
    texts: dict[str, str],
    packet: dict[str, set[int]],
) -> dict[str, set[int]]:
    extras: dict[str, set[int]] = {}
    for rel, view in views.items():
        if rel not in texts or view.sha256 != _sha(texts[rel]):
            continue
        fresh = (view.requested - view.shown) - packet.get(rel, set())
        if fresh:
            extras[rel] = fresh
    return extras


def _fill_round_robin(
    group: list[tuple[str, set[int]]],
    selection: dict[str, set[int]],
    render: Callable[[], str],
    budget: int,
) -> None:
    queues = {rel: sorted(lines - selection.get(rel, set())) for rel, lines in group}
    blocked: set[str] = set()
    while any(queues[rel] for rel in queues if rel not in blocked):
        for rel in list(queues):
            if rel in blocked or not queues[rel]:
                continue
            chunk, queues[rel] = queues[rel][:10], queues[rel][10:]
            before = set(selection.get(rel, set()))
            selection[rel] = before | set(chunk)
            if len(render()) > budget:
                selection[rel] = before
                blocked.add(rel)


def _format_body(
    order: list[str],
    texts: dict[str, str],
    notes: dict[str, str],
    missing: list[str],
    selection: dict[str, set[int]],
    marks: dict[str, list[int]],
    views: dict[str, FileView],
) -> str:
    rows: list[str] = []
    seen: set[str] = set()
    for rel in order + [name for name in notes if name not in order]:
        if rel in seen:
            continue
        seen.add(rel)
        if rel in notes:
            rows.append(notes[rel])
            continue
        if rel not in texts:
            continue
        text = texts[rel]
        pending: set[int] = set()
        view = views.get(rel)
        if view and view.sha256 == _sha(text):
            pending = (view.requested - view.shown) - selection.get(rel, set())
        if not selection.get(rel):
            if pending:
                rows.append(f"({len(pending)} requested lines of {rel} did not fit; read it again to see them)")
            continue
        total = len(text.splitlines())
        rows.append(f"{rel} ({total} lines, sha256 {_sha(text)[:12]})")
        rows.append(format_lines(text, selection[rel], marks=set(marks.get(rel, []))).rstrip("\n"))
        if pending:
            rows.append(
                f"({len(pending)} requested lines of {rel} did not fit; read it again to see them)"
            )
    if missing:
        listed = ", ".join(missing[:8]) + (f", and {len(missing) - 8} more" if len(missing) > 8 else "")
        rows.append(f"Cited files missing from the candidate: {listed}")
    return "\n".join(rows)


def _statement_span(lines: list[str], mark: int) -> set[int]:
    if not (1 <= mark <= len(lines)):
        return set()
    start = mark
    while start > 1:
        prev = lines[start - 2].rstrip()
        current = lines[start - 1].lstrip()
        continued = current.startswith((".", "?", ":", "+", "&&", "||", "`", "}", ")"))
        continued = continued or prev.endswith((",", "(", "{", "`", "&&", "||", "?"))
        if continued:
            start -= 1
            continue
        break
    end = mark
    while end < len(lines) and not lines[end - 1].rstrip().endswith((";", "{", "}", ";", ">", "/>")):
        end += 1
        if end - mark > 12:
            break
    return {number for number in range(start, end + 1) if 1 <= number <= len(lines)}


def _enclosing_span(lines: list[str], mark: int) -> set[int]:
    start = None
    for number in range(min(mark, len(lines)), 0, -1):
        if _DECL.search(lines[number - 1]) or re.search(r"on[A-Z]\w*\s*=\s*\{|=>\s*\{", lines[number - 1]):
            start = number
            break
    if start is None:
        return _window(mark, 12, len(lines))
    end = _brace_end(lines, start)
    if end - start + 1 > _SCOPE_CAP:
        kept = set(range(start, min(start + 2, len(lines)) + 1))
        kept |= _window(mark, 8, len(lines))
        kept.add(min(end, len(lines)))
        return kept
    return set(range(start, min(end, len(lines)) + 1))


def _jsx_span(lines: list[str], mark: int) -> set[int]:
    start = None
    tag = None
    low = max(1, mark - 40)
    for number in range(min(mark, len(lines)), low - 1, -1):
        stripped = lines[number - 1].lstrip()
        if stripped.startswith("</"):
            continue
        match = _OPEN_TAG.search(lines[number - 1])
        if not match:
            continue
        name = match.group(1)
        rest = lines[number - 1][match.end():]
        if "/>" in rest and "</" not in rest:
            continue
        start = number
        tag = name
        break
    if start is None or not tag:
        return set()
    depth = 0
    end = min(len(lines), mark + 30)
    for number in range(start, end + 1):
        line = lines[number - 1]
        depth += len(re.findall(rf"<{re.escape(tag)}\b", line))
        depth -= line.count(f"</{tag}>")
        if "/>" in line and f"<{tag}" in line:
            depth -= 1
        if number > start and depth <= 0:
            return set(range(start, number + 1))
    return set(range(start, min(end, len(lines)) + 1))


def _brace_end(lines: list[str], start: int) -> int:
    depth = 0
    started = False
    for number in range(start, len(lines) + 1):
        for char in lines[number - 1]:
            if char == "{":
                depth += 1
                started = True
            elif char == "}":
                depth -= 1
                if started and depth <= 0:
                    return number
    return min(len(lines), start + _SCOPE_CAP)


def _related_imports(hit: SourceHit, lines: list[str]) -> list[tuple[set[str], str]]:
    needed = _needed_names(hit, lines)
    related: list[tuple[set[str], str]] = []
    error_line = lines[hit.line - 1] if hit.line and 1 <= hit.line <= len(lines) else ""
    for names, spec, number in _imports(lines):
        on_error = hit.line is not None and number == hit.line
        mentions_spec = spec in hit.message or spec in error_line
        if on_error or mentions_spec or names & needed:
            related.append((names or needed, spec))
    return related


def _imports(lines: list[str]) -> list[tuple[set[str], str, int]]:
    found: list[tuple[set[str], str, int]] = []
    for number, line in enumerate(lines[:40], start=1):
        stripped = line.strip()
        if not stripped.startswith("import "):
            continue
        spec_match = re.search(r"""from\s+['"]([^'"]+)['"]""", stripped)
        if not spec_match:
            continue
        names: set[str] = set()
        named = re.search(r"\{([^}]+)\}", stripped)
        if named:
            for part in named.group(1).split(","):
                token = part.strip()
                if not token:
                    continue
                left, _, right = token.partition(" as ")
                names.add(left.replace("type", "").strip())
                if right:
                    names.add(right.strip())
        default = re.match(r"import\s+(?:type\s+)?([A-Za-z_]\w*)\s+", stripped)
        if default and default.group(1) not in {"type", "from"}:
            names.add(default.group(1))
        found.append(( {name for name in names if name}, spec_match.group(1), number ))
    return found


def _needed_names(hit: SourceHit, lines: list[str]) -> set[str]:
    names = set(_QUOTED.findall(hit.message))
    if hit.line and 1 <= hit.line <= len(lines):
        names.update(_IDENT.findall(lines[hit.line - 1]))
    return {name for name in names if name not in _JS_KW and len(name) > 1}


def _export_span(lines: list[str], names: set[str]) -> set[int]:
    if len(lines) <= _SMALL_FILE:
        return set(range(1, len(lines) + 1))
    chosen = set(_head(lines))
    want = {name for name in names if name}
    for number, line in enumerate(lines, start=1):
        match = _EXPORT.search(line)
        if not match:
            continue
        exported = match.group("name") if match.lastindex else None
        if exported in want or (want and "export default" in line):
            if "{" in line or "function" in line or "interface" in line:
                chosen |= _enclosing_span(lines, number)
            else:
                chosen.add(number)
            chosen |= _statement_span(lines, number)
    if len(chosen) <= 2 and want:
        chosen |= set(range(1, min(len(lines), _NAMED_LINES) + 1))
    return chosen


def _resolve_spec(root: Path, from_rel: str, spec: str) -> str | None:
    if not spec.startswith("."):
        return None
    parent = Path(from_rel).parent
    joined = Path(*(parent.parts + Path(spec).parts))
    parts: list[str] = []
    for part in joined.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in {".", ""}:
            parts.append(part)
    rel = "/".join(parts)
    candidates = [rel]
    if not Path(rel).suffix:
        candidates.extend(rel + suffix for suffix in (".ts", ".tsx", ".js", ".jsx"))
        candidates.extend(rel + suffix for suffix in ("/index.ts", "/index.tsx", "/index.js"))
    for candidate in candidates:
        if contained_file(root, candidate) and (root / candidate).is_file():
            return candidate
    return None


def _window(mark: int, radius: int, total: int) -> set[int]:
    return {number for number in range(mark - radius, mark + radius + 1) if 1 <= number <= total}


def _head(lines: list[str]) -> list[int]:
    head: list[int] = []
    for number, line in enumerate(lines[:_HEAD_MAX], start=1):
        stripped = line.strip()
        if stripped.startswith(("import ", "import{", "//", "/*", "*")) or not stripped:
            head.append(number)
            continue
        break
    while head and not lines[head[-1] - 1].strip():
        head.pop()
    return head


def _severity(hit: SourceHit) -> int:
    blob = f"{hit.code} {hit.message}".lower()
    if re.search(r"ts10\d{2}|ts11\d{2}|unexpected token|unmatched|failed to parse|syntax error", blob):
        return 0
    if re.search(r"ts2307|ts2305|ts2306|ts2614|ts1149|cannot resolve|cannot find module|no exported member", blob):
        return 1
    if "ts2779" in blob or "optional property access" in blob:
        return 2
    if "ts2304" in blob or "cannot find name" in blob:
        return 3
    if "ts1484" in blob or "type-only import" in blob:
        return 4
    if re.search(r"ts6133|ts6192|never (read|used)|are unused", blob):
        return 6
    return 5


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
    code_match = re.search(r"\b(TS\d+)\b", snippet)
    hits.append(
        SourceHit(
            rel=rel,
            line=number,
            column=col,
            message=_compact(snippet)[:400],
            code=code_match.group(1) if code_match else "",
        )
    )


def _project_rel(raw: str) -> str | None:
    text = (raw or "").replace("\\", "/").strip().strip("\"'")
    if "src/" in text:
        text = "src/" + text.split("src/", 1)[1]
    text = text.lstrip("./")
    return safe_rel(text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    return sorted(hits, key=lambda hit: (_severity(hit), hit.rel, hit.line or 0))
