"""Models Local Coder can switch between: installed local files plus one hosted model.

Local entries are a discovery list of files already on the Mac; this never
downloads weights. Bonsai 2 must use the PrismML llama-server; the Qwen files
use the ordinary llama.cpp binary on PATH. The hosted entry runs on NVIDIA's
servers and is available when its API key is on this Mac.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .hosted import find_api_key
from .machine import MachineConfig, load_machine_config


class CatalogEntry(BaseModel):
    """One selectable model."""

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
    provider: str = "local"
    endpoint: str = ""
    api_key_name: str = ""
    temperature: float = 0.0
    top_p: float = 0.0


def _home() -> Path:
    return Path.home()


def known_entries(home: Path | None = None) -> list[CatalogEntry]:
    """Hard-coded catalog of models this product knows how to launch or call."""
    root = home or _home()
    qwen_dir = root / ".cache" / "local-coder" / "models"
    bonsai_dir = root / "Models" / "Bonsai-demo"
    return [
        CatalogEntry(
            id="nemotron35-super",
            name="Nemotron 3.5 Super 120B — NVIDIA cloud",
            model_file="",
            model_alias="nvidia/nemotron-3.5-super-vl-120b-a12b",
            server_binary="",
            n_ctx=131072,
            quantization="hosted",
            note=(
                "Runs on NVIDIA's servers. It can read your folders (never secrets or ~/Library) and run "
                "sandboxed commands; what it reads is sent to NVIDIA. Changes still wait for Keep."
            ),
            max_tokens=16384,
            gen_timeout=300,
            provider="nvidia",
            endpoint="https://integrate.api.nvidia.com",
            api_key_name="NVIDIA_API_KEY",
        ),
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


def gpu_entries() -> list[CatalogEntry]:
    """Models in your GPU library (from the last-seen models.json; never wakes the GPU)."""
    from .gpu.cloud import cached_library, load_config

    cfg = load_config()
    if cfg is None:
        return []
    hourly = cfg.get("hourly_usd")
    rows: list[CatalogEntry] = []
    for row in cached_library().get("models", []):
        rows.append(CatalogEntry(
            id=f"gpu:{row['id']}",
            name=f"{row.get('name') or row['id']} — your GPU",
            model_file="",
            model_alias=str(row["id"]),
            server_binary="",
            n_ctx=int(row.get("context") or 32768),
            quantization="gpu",
            note=(
                f"Runs on your Google Cloud GPU (about ${hourly}/hour while it is on; it switches off by itself "
                "after about 15 idle minutes). The first request after a pause takes a few minutes to start it."
            ),
            max_tokens=min(int(row.get("max_tokens") or 8192), 32768),
            gen_timeout=900,
            provider="cloudrun",
            endpoint=str(cfg["url"]),
            temperature=float(row.get("temperature") or 0.0),
            top_p=float(row.get("top_p") or 0.0),
        ))
    return rows


def hosted_ready(entry: CatalogEntry) -> bool:
    """Whether a hosted entry can be used now (no network call)."""
    if entry.provider == "cloudrun":
        return bool(entry.endpoint)
    return bool(find_api_key(entry.api_key_name)[0])


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
    for entry in [*known_entries(home), *gpu_entries()]:
        if entry.provider != "local":
            available = hosted_ready(entry)
            is_selected = current.provider == entry.provider and current.model_alias == entry.model_alias
        else:
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
    for entry in [*known_entries(home), *gpu_entries()]:
        if entry.id == model_id:
            return entry
    return None
