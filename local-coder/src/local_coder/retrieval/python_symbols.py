"""Python AST symbol definitions for structural retrieval."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Symbol:
    """One named definition with its source range."""

    path: str
    name: str
    kind: str  # function | class
    start: int
    end: int


def index_symbols(root: Path, files: list[str]) -> dict[str, list[Symbol]]:
    """Index top-level and nested def/class statements via ast."""
    index: dict[str, list[Symbol]] = {}

    def visit(path: str, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                kind = "function"
            elif isinstance(child, ast.ClassDef):
                kind = "class"
            else:
                visit(path, child)
                continue
            end = getattr(child, "end_lineno", None) or child.lineno
            index.setdefault(child.name.lower(), []).append(
                Symbol(path, child.name, kind, child.lineno, end)
            )
            visit(path, child)

    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
        except (OSError, SyntaxError, ValueError):
            continue
        visit(rel, tree)
    return index


def match_symbols(index: dict[str, list[Symbol]], names: list[str]) -> list[Symbol]:
    """Match requested symbol names exactly, then by substring."""
    matched: list[Symbol] = []
    wanted = [n.strip().lower() for n in names if n and n.strip()][:20]
    for name in wanted:
        if name in index:
            matched.extend(index[name])
    for name in wanted:
        if name in index:
            continue
        for key, symbols in index.items():
            if name in key or key in name:
                matched.extend(symbols)
    return matched
