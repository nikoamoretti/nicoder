"""Runtime-bound source spans for protocol v3 PATCH replace_span.

A span is an exact byte interval of a focus file, bound to the requesting
turn: the model selects spans by ID and writes only new code. Spans are
statements and short structural units; the runtime never fuzzes, infers, or
repairs them.

Boundary contract (exact bytes replaced):
- A span that begins its own line starts at the first byte of that line, so
  its displayed text includes the statement's indentation. A statement that
  shares a line with preceding code starts at the statement's first token.
- A span ends at the last byte of the statement. If only whitespace or a
  trailing comment follows on the final line, the span extends to the end of
  that line, excluding the line break. Trailing line breaks are never part
  of a span. Replacement text is written verbatim; the runtime performs no
  indentation or newline adjustment.
- Decorators and comments on lines before a statement are outside its span
  and are preserved byte-for-byte by a replacement.
- IDs (S1, S2, ...) are positional over one request's catalogue only. They
  are snapshotted with path, full file hash, exact interval, and original
  bytes hash before inference and resolved against that snapshot, so an ID
  can never silently mean something else on another turn or after edits.
- Catalogue order is deterministic: within a file, child statements are
  listed before their enclosing compound, so small editing units cannot be
  crowded out by large parents under a budget cap.
- Only permitted, editable source paths receive span IDs. Read-only context
  (tests, configuration) remains visible through retrieval excerpts but is
  never presentable as a PATCH target. Files that fail to parse, symlinks,
  escaping paths, oversized atomic statements without children, and spans
  beyond budget or count are omitted with a recorded reason.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .paths import matches_any

SPAN_CATALOGUE_VERSION = "span-catalogue-v2"
MAX_SPAN_BYTES = 2048
MAX_SPANS = 48
MAX_SPAN_DEPTH = 12

_KIND_NAMES: dict[type, str] = {
    ast.ClassDef: "class",
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "function",
    ast.If: "if",
    ast.For: "for",
    ast.AsyncFor: "for",
    ast.While: "while",
    ast.With: "with",
    ast.AsyncWith: "with",
    ast.Try: "try",
    ast.Return: "return",
    ast.Assign: "assign",
    ast.AnnAssign: "assign",
    ast.AugAssign: "assign",
    ast.Expr: "expression",
    ast.Raise: "raise",
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.Assert: "assert",
    ast.Delete: "delete",
    ast.Global: "global",
    ast.Nonlocal: "nonlocal",
    ast.Pass: "pass",
    ast.Break: "break",
    ast.Continue: "continue",
}


def _has_try_star() -> bool:
    return hasattr(ast, "TryStar")


def _compound_kinds() -> tuple[type, ...]:
    kinds: tuple[type, ...] = (
        ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For,
        ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
    )
    if _has_try_star():
        kinds = kinds + (ast.TryStar,)  # type: ignore[attr-defined]
    if hasattr(ast, "Match"):
        kinds = kinds + (ast.Match,)
    return kinds


@dataclass
class Span:
    """One editable byte interval with its content identity and structure."""

    span_id: str
    path: str
    start_byte: int
    end_byte: int
    file_sha256: str
    bytes_sha256: str
    kind: str
    enclosing: str

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
    """Exact byte interval for a statement under the documented contract."""
    end_lineno = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_lineno is None or end_col is None:
        return None
    try:
        line_start = offsets[node.lineno - 1]
        token_start = line_start + node.col_offset
        end = offsets[end_lineno - 1] + end_col
    except IndexError:
        return None
    if not 0 <= token_start < end <= len(data):
        return None
    prefix = data[line_start:token_start]
    start = line_start if prefix.strip(b" \t") == b"" else token_start
    line_end = data.find(b"\n", end)
    if line_end < 0:
        line_end = len(data)
    tail = data[end:line_end]
    if tail.strip(b" \t") == b"" or tail.lstrip(b" \t").startswith(b"#"):
        end = line_end
    if not 0 <= start < end <= len(data):
        return None
    try:
        data[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return (start, end)


def _child_statements(node: ast.stmt) -> list[ast.stmt]:
    """Direct body statements for compound nodes, including handler bodies."""
    if not isinstance(node, _compound_kinds()):
        return []
    children: list[ast.stmt] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list):
            children.extend(item for item in value if isinstance(item, ast.stmt))
    handlers = getattr(node, "handlers", None)
    if isinstance(handlers, list):
        for handler in handlers:
            body = getattr(handler, "body", None)
            if isinstance(body, list):
                children.extend(item for item in body if isinstance(item, ast.stmt))
    cases = getattr(node, "cases", None)
    if isinstance(cases, list):
        for case in cases:
            body = getattr(case, "body", None)
            if isinstance(body, list):
                children.extend(item for item in body if isinstance(item, ast.stmt))
    return children


def _kind(node: ast.stmt, parent_is_class: bool) -> str:
    name = _KIND_NAMES.get(type(node))
    if name is None:
        name = type(node).__name__.lower()
    if name == "function" and parent_is_class:
        return "method"
    return name


def _symbol_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name
    return None


def _collect(
    node: ast.stmt,
    data: bytes,
    offsets: list[int],
    max_span_bytes: int,
    out: list[tuple[int, int, str, str]],
    stack: tuple[str, ...],
    parent_is_class: bool,
    depth: int,
    omitted: list[str],
    rel: str,
) -> None:
    """Collect a statement and its useful children (children first)."""
    span = _node_span(data, offsets, node)
    if span is None:
        return
    start, end = span
    kind = _kind(node, parent_is_class)
    enclosing = ".".join(stack) if stack else "module"
    children = _child_statements(node)
    if end - start > max_span_bytes and not children:
        omitted.append(
            f"{rel} ({kind} at bytes {start}-{end} exceeds {max_span_bytes} "
            f"and has no child statements)")
        return
    if depth < MAX_SPAN_DEPTH:
        symbol = _symbol_name(node)
        child_stack = stack + (symbol,) if symbol else stack
        for child in children:
            _collect(child, data, offsets, max_span_bytes, out, child_stack,
                     isinstance(node, ast.ClassDef), depth + 1, omitted, rel)
    if end - start <= max_span_bytes:
        out.append((start, end, kind, enclosing))


def build_catalogue(
    root: Path,
    focus: list[tuple[str, int, int]],
    budget_chars: int,
    editable: list[str] | None = None,
    max_span_bytes: int = MAX_SPAN_BYTES,
    max_spans: int = MAX_SPANS,
) -> tuple[list[Span], list[str], dict[str, dict[str, str]]]:
    """Build the deterministic span catalogue for one request.

    Args:
        root: candidate tree root.
        focus: (path, start_line, end_line) excerpt ranges, in focus order.
        budget_chars: total span-text budget; catalogue stops with the rest
            recorded as omitted.
        editable: glob policy for permitted source files. None means no file
            is editable and the catalogue is empty (fail-safe).
        max_span_bytes: atomic statements larger than this are omitted unless
            they have child statements, which are offered instead.
        max_spans: hard cap on catalogue size.

    Returns (spans, omitted, bindings) where bindings maps span_id to its
    persisted identity record. Only fully displayed, editable spans are
    addressable.
    """
    editable = editable or []
    spans: list[Span] = []
    omitted: list[str] = []
    used_chars = 0
    counter = 0
    root_resolved = root.resolve()

    for rel, first_line, last_line in focus:
        if not matches_any(rel, editable):
            omitted.append(f"{rel} (read-only path: no editable spans)")
            continue
        full = root / rel
        if full.is_symlink():
            omitted.append(f"{rel} (symlink rejected)")
            continue
        try:
            resolved = full.resolve()
            resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            omitted.append(f"{rel} (path escapes the candidate tree)")
            continue
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
        intervals: list[tuple[int, int, str, str]] = []
        for node in tree.body:
            if not isinstance(node, ast.stmt):
                continue
            end_lineno = getattr(node, "end_lineno", None) or node.lineno
            if end_lineno < first_line or node.lineno > last_line:
                continue
            _collect(node, data, offsets, max_span_bytes, intervals, (), False, 0,
                     omitted, rel)
        for start, end, kind, enclosing in intervals:
            if len(spans) >= max_spans:
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
                kind=kind,
                enclosing=enclosing,
            ))
            used_chars += len(span_text)
    bindings = {
        span.span_id: {
            "path": span.path,
            "file_sha256": span.file_sha256,
            "start_byte": str(span.start_byte),
            "end_byte": str(span.end_byte),
            "bytes_sha256": span.bytes_sha256,
            "kind": span.kind,
            "enclosing": span.enclosing,
        }
        for span in spans
    }
    return spans, omitted, bindings


def render_spans(root: Path, spans: list[Span]) -> str:
    """Render fully-displayed span blocks with exact (raw) source text."""
    blocks = []
    for span in spans:
        try:
            text = (root / span.path).read_bytes()[span.start_byte:span.end_byte].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            text = "<unreadable>"
        blocks.append(
            f"[{span.span_id}] {span.path} bytes {span.start_byte}-{span.end_byte} "
            f"sha={span.bytes_sha256[:12]} kind={span.kind} enclosing={span.enclosing}\n{text}"
        )
    return "\n".join(blocks)