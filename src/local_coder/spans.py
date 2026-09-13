"""Runtime-bound source spans for protocol v3 PATCH replace_span.

A span is an exact byte interval of a focus file, bound to the requesting
turn: the model selects spans by ID and writes only new code. Spans are
complete individual statements (or short structural units); the runtime
never fuzzes, infers, or repairs them.

Boundary contract (documented precisely):
- Intervals are byte offsets into the file as read (UTF-8). A span covers
  exactly one statement's bytes, from its first byte through its last byte,
  excluding any trailing newline. Newlines, indentation, blank lines, and
  comments outside the interval are preserved verbatim on replacement.
- IDs (S1, S2, ...) are positional over one request's catalogue only. They
  are snapshotted with path, full file hash, exact interval, and original
  bytes hash before inference and resolved against that snapshot, so an ID
  can never silently mean something else on another turn or after edits.
- Only spans fully displayed in the briefing section are editable. Files
  that fail to parse, spans over budget, and truncated ranges are omitted
  with a recorded reason and are never addressable.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_SPAN_BYTES = 2048
MAX_SPANS = 48

_COMPOUND_WITH_BODY = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)


def _has_try_star() -> bool:
    return hasattr(ast, "TryStar")


@dataclass
class Span:
    """One editable byte interval with its content identity."""

    span_id: str
    path: str
    start_byte: int
    end_byte: int
    file_sha256: str
    bytes_sha256: str

    def text(self, data: bytes) -> str:
        """Slice the span bytes (validated at build time)."""
        return data[self.start_byte:self.end_byte].decode("utf-8")


def _line_offsets(data: bytes) -> list[int]:
    """Start byte of every 1-based line."""
    offsets = [0]
    for index, byte in enumerate(data):
        if byte == 0x0A:
            offsets.append(index + 1)
    return offsets


def _node_span(data: bytes, offsets: list[int], node: ast.stmt) -> tuple[int, int] | None:
    """Byte interval for a statement, or None when positions are unusable."""
    end_lineno = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_lineno is None or end_col is None:
        return None
    try:
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[end_lineno - 1] + end_col
    except IndexError:
        return None
    if not 0 <= start < end <= len(data):
        return None
    try:
        data[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return (start, end)


def _child_statements(node: ast.stmt) -> list[ast.stmt]:
    """Direct body statements for compound nodes, else empty."""
    kinds: tuple[type, ...] = _COMPOUND_WITH_BODY
    if _has_try_star():
        kinds = kinds + (ast.TryStar,)  # type: ignore[attr-defined]
    if not isinstance(node, kinds):
        return []
    children: list[ast.stmt] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list):
            children.extend(item for item in value if isinstance(item, ast.stmt))
    return children


def _collect(node: ast.stmt, data: bytes, offsets: list[int], max_span_bytes: int,
             out: list[tuple[int, int]]) -> None:
    """Append this statement, or descend once into oversized compounds."""
    span = _node_span(data, offsets, node)
    if span is None:
        return
    start, end = span
    if end - start <= max_span_bytes:
        out.append(span)
        return
    children = _child_statements(node)
    if not children:
        return
    for child in children:
        _collect(child, data, offsets, max_span_bytes, out)


def build_catalogue(
    root: Path,
    focus: list[tuple[str, int, int]],
    budget_chars: int,
    max_span_bytes: int = MAX_SPAN_BYTES,
    max_spans: int = MAX_SPANS,
) -> tuple[list[Span], list[str], dict[str, dict[str, str]]]:
    """Build the deterministic span catalogue for one request.

    Args:
        root: candidate tree root.
        focus: (path, start_line, end_line) excerpt ranges, in focus order.
        budget_chars: total span-text budget; catalogue stops with the rest
            recorded as omitted.
        max_span_bytes: statements larger than this descend into direct
            child statements; atomic oversized statements are omitted.
        max_spans: hard cap on catalogue size.

    Returns (spans, omitted, bindings) where bindings maps span_id to its
    persisted identity record. Only fully displayed spans are addressable.
    """
    spans: list[Span] = []
    omitted: list[str] = []
    used_chars = 0
    counter = 0

    def want_more() -> bool:
        return len(spans) < max_spans

    for rel, first_line, last_line in focus:
        full = root / rel
        try:
            data = full.read_bytes()
        except OSError as exc:
            omitted.append(f"{rel} (unreadable: {exc})")
            continue
        if b"\0" in data[:8192]:
            omitted.append(f"{rel} (binary file has no spans)")
            continue
        if b"\r\n" in data:
            omitted.append(f"{rel} (CRLF line endings unsupported)")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append(f"{rel} (non-UTF8 file has no spans)")
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            omitted.append(f"{rel} (unparseable: {exc})")
            continue
        file_sha = hashlib.sha256(data).hexdigest()
        offsets = _line_offsets(data)
        intervals: list[tuple[int, int]] = []
        for node in tree.body:
            if not isinstance(node, ast.stmt):
                continue
            end_lineno = getattr(node, "end_lineno", None) or node.lineno
            if end_lineno < first_line or node.lineno > last_line:
                continue
            _collect(node, data, offsets, max_span_bytes, intervals)
        intervals.sort()
        for start, end in intervals:
            if not want_more():
                omitted.append(f"{rel} (catalogue span cap reached)")
                break
            span_text = data[start:end].decode("utf-8")
            if used_chars + len(span_text) > budget_chars and spans:
                omitted.append(f"{rel} (span catalogue budget reached)")
                break
            counter += 1
            span_id = f"S{counter}"
            spans.append(Span(
                span_id=span_id,
                path=rel,
                start_byte=start,
                end_byte=end,
                file_sha256=file_sha,
                bytes_sha256=hashlib.sha256(data[start:end]).hexdigest(),
            ))
            used_chars += len(span_text)
    bindings = {
        span.span_id: {
            "path": span.path,
            "file_sha256": span.file_sha256,
            "start_byte": str(span.start_byte),
            "end_byte": str(span.end_byte),
            "bytes_sha256": span.bytes_sha256,
        }
        for span in spans
    }
    return spans, omitted, bindings


def render_spans(root: Path, spans: list[Span]) -> str:
    """Render fully-displayed span blocks with raw (unnumbered) source text."""
    blocks = []
    for span in spans:
        try:
            text = (root / span.path).read_bytes()[span.start_byte:span.end_byte].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            text = "<unreadable>"
        blocks.append(
            f"[{span.span_id}] {span.path} bytes {span.start_byte}-{span.end_byte} "
            f"sha={span.bytes_sha256[:12]}\n{text}"
        )
    return "\n".join(blocks)
