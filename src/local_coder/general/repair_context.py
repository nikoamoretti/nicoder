"""Structured repair state: keep the current diagnostic and its source in view."""
from __future__ import annotations

import hashlib
import json
import re
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
_LOCATION = re.compile(r"\(\d+,\d+\)|:\d+:\d+")
_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b"
    r"|^\s*(?:export\s+)?(?:const|let)\s+\w+\s*(?::[^=]+)?=\s*(?:async\s*)?"
    r"(?:\([^)]*\)\s*(?::[^=]+)?=>|function\b|use(?:Callback|Memo)\b)"
    r"|^\s*(?:export\s+)?(?:interface|type|class|enum)\s+\w+"
)
_ERROR_RADIUS = 3
_READ_CHUNK = 100
_HEAD_MAX = 12
_NAMED_LINES = 24
_DIAG_BUDGET = 2400


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


@dataclass
class FileView:
    """Lines of one candidate file the model asked for or was shown, for one content identity."""

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
    for hit in hits:
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


def error_lines(state: RepairState) -> dict[str, list[int]]:
    """Cited lines per file, in hit priority. Files cited without a line map to []."""
    marks: dict[str, list[int]] = {}
    for hit in state.hits:
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


def render_source(
    state: RepairState,
    root: Path,
    views: dict[str, FileView] | None,
    *,
    budget: int,
) -> tuple[str, dict[str, set[int]]]:
    """Cited source and requested reads within budget. Error lines are the last thing dropped."""
    views = views or {}
    marks = error_lines(state)
    weak = _weak_lines(state)
    order = list(marks) + [rel for rel in views if rel not in marks]
    texts: dict[str, str] = {}
    notes: dict[str, str] = {}
    missing: list[str] = []
    for rel in order:
        path = contained_file(root, rel)
        if path is None:
            notes[rel] = f"{rel}: refused, the path resolves outside the candidate"
            continue
        if not path.is_file():
            missing.append(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            notes[rel] = f"{rel}: file could not be read"
            continue
        recorded = state.source_identity.get(rel)
        if state.stale and recorded and recorded != _sha(text):
            notes[rel] = f"{rel}: source changed after the check; this error is not current until rechecked"
            continue
        texts[rel] = text
    selection: dict[str, set[int]] = {rel: set() for rel in texts}
    layers = _layers(marks, weak, views, texts, order)

    def render() -> str:
        rows: list[str] = []
        for rel in order:
            if rel in notes:
                rows.append(notes[rel])
                continue
            if rel not in texts:
                continue
            text = texts[rel]
            view = views.get(rel)
            pending: set[int] = set()
            if view and view.sha256 == _sha(text):
                pending = (view.requested - view.shown) - selection.get(rel, set())
            note = f"{len(pending)} requested lines of {rel} did not fit; read it again to see them"
            if not selection.get(rel):
                if pending:
                    rows.append(f"({note})")
                continue
            total = len(text.splitlines())
            rows.append(f"{rel} ({total} lines, sha256 {_sha(text)[:12]})")
            rows.append(format_lines(text, selection[rel], marks=set(marks.get(rel, []))).rstrip("\n"))
            if pending:
                rows.append(f"({note})")
        if missing:
            listed = ", ".join(missing[:8]) + (f", and {len(missing) - 8} more" if len(missing) > 8 else "")
            rows.append(f"Cited files missing from the candidate: {listed}")
        return "\n".join(rows)

    def fits_whole(rel: str, lines: set[int]) -> None:
        before = set(selection[rel])
        selection[rel] = before | lines
        if len(render()) > budget:
            selection[rel] = before

    def fill_round_robin(group: list[tuple[str, set[int]]]) -> None:
        queues = {rel: sorted(lines - selection[rel]) for rel, lines in group}
        blocked: set[str] = set()
        while any(queues[rel] for rel in queues if rel not in blocked):
            for rel in list(queues):
                if rel in blocked or not queues[rel]:
                    continue
                chunk, queues[rel] = queues[rel][:10], queues[rel][10:]
                before = set(selection[rel])
                selection[rel] = before | set(chunk)
                if len(render()) > budget:
                    selection[rel] = before
                    blocked.add(rel)

    index = 0
    while index < len(layers):
        rel, lines, mode = layers[index]
        if mode == "mandatory":
            selection[rel] |= lines
            index += 1
            continue
        if mode == "partial":
            group: list[tuple[str, set[int]]] = []
            while index < len(layers) and layers[index][2] == "partial":
                group.append((layers[index][0], layers[index][1]))
                index += 1
            fill_round_robin(group)
            continue
        fits_whole(rel, lines)
        index += 1
    return render(), {rel: lines for rel, lines in selection.items() if lines}


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
    """Header, exact diagnostic, and source within budget. Returns rendered text and shown lines."""
    head = [f"Current blocking check: {state.check}"]
    if status:
        head.append(status)
    diag_budget = min(_DIAG_BUDGET, max(600, budget // 3))
    head.append("Exact diagnostic:")
    head.append(diagnostic_text(state, diag_budget) or "(no diagnostic text)")
    if state.hits:
        loc = state.hits[0]
        where = loc.rel
        if loc.line:
            where += f":{loc.line}"
            if loc.column:
                where += f":{loc.column}"
        head.append(f"First source location: {where}")
    if state.candidate_id:
        head.append(f"Candidate identity: {state.candidate_id}")
    if state.changed_since:
        head.append("Since the previous check: " + state.changed_since)
    if state.stale:
        head.append(
            "This diagnostic is stale. The cited source changed after the check. Do not treat it as current."
        )
    head.append("Source (hash|line; > marks an error line; … marks lines not shown):")
    header = "\n".join(head) + "\n"
    shown: dict[str, set[int]] = {}
    if root is None:
        body = "\n".join(state.excerpts)
    else:
        body, shown = render_source(state, root, views, budget=max(800, budget - len(header)))
    return header + body + "\n", shown


def write_repair_state(request_root: Path, state: RepairState) -> None:
    """Persist repair state next to the request, outside the model prompt."""
    payload = asdict(state)
    (request_root / "repair_state.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8",
    )


def _layers(
    marks: dict[str, list[int]],
    weak: dict[str, set[int]],
    views: dict[str, FileView],
    texts: dict[str, str],
    order: list[str],
) -> list[tuple[str, set[int], str]]:
    """Priority order: error lines, fresh reads, error context, unused-name context, reads already seen."""
    layers: list[tuple[str, set[int], str]] = []
    current = {
        rel: view for rel, view in views.items()
        if rel in texts and view.sha256 == _sha(texts[rel]) and view.requested
    }
    for rel, lines in marks.items():
        if rel not in texts:
            continue
        total = len(texts[rel].splitlines())
        chosen: set[int] = set()
        if not lines:
            chosen |= set(range(1, min(total, _NAMED_LINES) + 1))
        for mark in lines:
            chosen |= _window(mark, 0 if mark in weak.get(rel, set()) else 1, total)
        layers.append((rel, chosen, "mandatory"))
    for rel in order:
        if rel in current and current[rel].requested - current[rel].shown:
            layers.append((rel, current[rel].requested - current[rel].shown, "partial"))
    for rel, lines in marks.items():
        if rel not in texts:
            continue
        source = texts[rel].splitlines()
        chosen = set(_head(source))
        for mark in lines:
            if mark in weak.get(rel, set()):
                continue
            chosen |= _window(mark, _ERROR_RADIUS, len(source))
            chosen |= _decl_lines(source, mark)
        layers.append((rel, chosen, "whole"))
    for rel, lines in marks.items():
        if rel not in texts:
            continue
        source = texts[rel].splitlines()
        chosen = set()
        for mark in lines:
            if mark not in weak.get(rel, set()):
                continue
            chosen |= _window(mark, 2, len(source))
            chosen |= _decl_lines(source, mark)
        if chosen:
            layers.append((rel, chosen, "whole"))
    for rel in order:
        if rel in current and current[rel].requested & current[rel].shown:
            layers.append((rel, current[rel].requested & current[rel].shown, "partial"))
    return layers


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


def _decl_lines(lines: list[str], mark: int) -> set[int]:
    for number in range(min(mark, len(lines)), 0, -1):
        if _DECL.search(lines[number - 1]):
            return {n for n in (number, number + 1) if n <= len(lines)}
    return set()


def _weak_lines(state: RepairState) -> dict[str, set[int]]:
    weak: dict[str, set[int]] = {}
    strong: dict[str, set[int]] = {}
    for hit in state.hits:
        if not hit.line:
            continue
        bucket = weak if _WEAK_ERROR.search(hit.message) else strong
        bucket.setdefault(hit.rel, set()).add(hit.line)
    return {rel: lines - strong.get(rel, set()) for rel, lines in weak.items()}


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
    return sorted(hits, key=lambda hit: (1 if _WEAK_ERROR.search(hit.message) else 0, hit.rel, hit.line or 0))
