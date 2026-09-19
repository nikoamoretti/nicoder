"""Installed local models that Local Coder can switch between.

This is a discovery list of files already on the Mac. It never downloads
weights. Bonsai 2 must use the PrismML llama-server; the Qwen files use the
ordinary llama.cpp binary on PATH.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .machine import MachineConfig, load_machine_config


class CatalogEntry(BaseModel):
    """One selectable local model."""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    model_file: str
    model_alias: str
    server_binary: str
    n_ctx: int
    quantization: str
    note: str
    max_tokens: int = 2048
    gen_timeout: int = 300


def _home() -> Path:
    return Path.home()


def known_entries(home: Path | None = None) -> list[CatalogEntry]:
    """Hard-coded catalog of models this product knows how to launch."""
    root = home or _home()
    qwen_dir = root / ".cache" / "local-coder" / "models"
    bonsai_dir = root / "Models" / "Bonsai-demo"
    return [
        CatalogEntry(
            id="bonsai2-27b",
            name="Bonsai 2 27B — preferred",
            model_file=str(
                bonsai_dir / "models" / "bonsai2-gguf" / "27B" / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
            ),
            model_alias="Ternary-Bonsai-2-27B-PQ2_0",
            server_binary=str(bonsai_dir / "bin" / "mac" / "llama-server"),
            n_ctx=8192,
            quantization="PQ2_0",
            note="Preferred. Writes real TypeScript. Slow on 16GB; leave it loaded.",
            max_tokens=3072,
            gen_timeout=600,
        ),
        CatalogEntry(
            id="qwen35-9b",
            name="Qwen 3.5 9B — faster fallback",
            model_file=str(qwen_dir / "Qwen_Qwen3.5-9B-Q4_K_M.gguf"),
            model_alias="Qwen3.5-9B-Q4_K_M",
            server_binary="llama-server",
            n_ctx=16384,
            quantization="Q4_K_M",
            note="Use only when Bonsai is too slow. Ordinary llama.cpp server.",
        ),
        CatalogEntry(
            id="qwen35-4b",
            name="Qwen 3.5 4B — fastest fallback",
            model_file=str(qwen_dir / "Qwen_Qwen3.5-4B-Q4_K_M.gguf"),
            model_alias="Qwen3.5-4B-Q4_K_M",
            server_binary="llama-server",
            n_ctx=8192,
            quantization="Q4_K_M",
            note="Smallest Qwen. Fast, weaker code. Ordinary llama.cpp server.",
            gen_timeout=180,
        ),
    ]


def _binary_ok(server_binary: str) -> bool:
    if shutil.which(server_binary):
        return True
    return Path(server_binary).expanduser().is_file()


def list_models(cfg: MachineConfig | None = None, *, home: Path | None = None) -> dict:
    """Models on this Mac plus which one is currently selected."""
    current = cfg or load_machine_config()
    current_file = ""
    if current.model_file:
        try:
            current_file = str(Path(current.model_file).expanduser().resolve())
        except OSError:
            current_file = current.model_file
    selected = ""
    rows: list[dict] = []
    for entry in known_entries(home):
        path = Path(entry.model_file)
        available = path.is_file() and _binary_ok(entry.server_binary)
        resolved = str(path.resolve()) if path.exists() else entry.model_file
        is_selected = bool(current_file) and resolved == current_file
        if is_selected:
            selected = entry.id
        rows.append({
            **entry.model_dump(),
            "available": available,
            "selected": is_selected,
        })
    return {
        "models": rows,
        "selected": selected,
        "current_alias": current.model_alias,
        "current_file": current.model_file,
    }


def entry_by_id(model_id: str, *, home: Path | None = None) -> CatalogEntry | None:
    """Look up one catalog id."""
    for entry in known_entries(home):
        if entry.id == model_id:
            return entry
    return None
