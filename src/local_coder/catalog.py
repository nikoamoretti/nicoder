"""Models Nicoder can switch between: installed local files plus hosted models.

Local entries are a discovery list of files already on the Mac; this never
downloads weights. Bonsai 2 must use the PrismML llama-server; the Qwen files
use the ordinary llama.cpp binary on PATH. Nemotron runs on NVIDIA's servers.
OrcaRouter models (`orca:<model id>`) come from OrcaRouter's cached model list:
the menu shows the selected and recently used ones, and any other can be picked
from the full list. Hosted entries are available when their API key is on this Mac.
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
    reasoning_switch: bool = False


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
            name="Nemotron 3.5 Super 120B",
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
            reasoning_switch=True,
        ),
        CatalogEntry(
            id="bonsai2-27b",
            name="Bonsai 2 27B",
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
            name="Qwen 3.5 9B",
            model_file=str(qwen_dir / "Qwen_Qwen3.5-9B-Q4_K_M.gguf"),
            model_alias="Qwen3.5-9B-Q4_K_M",
            server_binary="llama-server",
            n_ctx=16384,
            quantization="Q4_K_M",
            note="Use only when Bonsai is too slow. Ordinary llama.cpp server.",
        ),
        CatalogEntry(
            id="qwen35-4b",
            name="Qwen 3.5 4B",
            model_file=str(qwen_dir / "Qwen_Qwen3.5-4B-Q4_K_M.gguf"),
            model_alias="Qwen3.5-4B-Q4_K_M",
            server_binary="llama-server",
            n_ctx=8192,
            quantization="Q4_K_M",
            note="Smallest Qwen. Fast, weaker code. Ordinary llama.cpp server.",
            gen_timeout=180,
        ),
    ]


ORCA_PREFIX = "orca:"
_ORCA_CONTEXT_CAP = 131072
_ORCA_OUTPUT_CAP = 16384


def orca_entry(model_id: str) -> CatalogEntry:
    """A catalog entry for one OrcaRouter model (facts from the cached list when it has them)."""
    from . import orcarouter

    row = orcarouter.find_model(model_id) or {}
    name = str(row.get("name") or model_id.split("/", 1)[-1])
    provider = str(row.get("provider") or model_id.split("/", 1)[0])
    description = str(row.get("description") or "").strip()
    note = (f"{description} " if description else "") + (
        f"Runs through OrcaRouter: what it reads is sent to OrcaRouter and {provider}. "
        f"{orcarouter.price_label(row)} per million tokens." if row else
        "Runs through OrcaRouter: what it reads is sent to OrcaRouter and the model's provider."
    )
    return CatalogEntry(
        id=f"{ORCA_PREFIX}{model_id}",
        name=name,
        model_file="",
        model_alias=model_id,
        server_binary="",
        n_ctx=min(int(row.get("context") or _ORCA_CONTEXT_CAP), _ORCA_CONTEXT_CAP),
        quantization="hosted",
        note=note.replace(" Free per million tokens.", " Free."),
        max_tokens=min(int(row.get("max_output") or _ORCA_OUTPUT_CAP), _ORCA_OUTPUT_CAP),
        gen_timeout=600,
        provider="orcarouter",
        endpoint=orcarouter.API,
        api_key_name=orcarouter.KEY_NAME,
        reasoning_switch=bool(row.get("reasoning_switch")) or orcarouter.has_reasoning_switch(model_id, None),
    )


def orca_entries(current: MachineConfig) -> list[CatalogEntry]:
    """OrcaRouter models for the menu: the selected one, recent ones, else a suggested few."""
    from . import orcarouter

    selected = current.model_alias if current.provider == "orcarouter" else ""
    return [orca_entry(model_id) for model_id in orcarouter.menu_models(selected)]


def hosted_ready(entry: CatalogEntry) -> bool:
    """Whether a hosted entry can be used now (no network call)."""
    return bool(find_api_key(entry.api_key_name)[0])


GROUPS = {"local": "On This Mac", "nvidia": "NVIDIA Cloud", "orcarouter": "OrcaRouter"}


def subtitle(entry: CatalogEntry) -> str:
    """One short line under the model name in the picker."""
    if entry.provider == "orcarouter":
        from . import orcarouter

        row = orcarouter.find_model(entry.model_alias) or {}
        return orcarouter.price_label(row) if row else "OrcaRouter"
    if entry.provider == "nvidia":
        return "Free, hosted by NVIDIA"
    return {"bonsai2-27b": "Best local quality, slow", "qwen35-9b": "Faster, lighter",
            "qwen35-4b": "Fastest, weakest"}.get(entry.id, "Runs on this Mac")


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
    for entry in [*known_entries(home), *orca_entries(current)]:
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
            "group": GROUPS.get(entry.provider, entry.provider),
            "subtitle": subtitle(entry),
        })
        if entry.provider == "orcarouter":
            from . import orcarouter

            facts = orcarouter.find_model(entry.model_alias) or {}
            rows[-1].update({key: facts.get(key) for key in ("free", "intelligence", "context", "speed")})
    return {
        "models": rows,
        "selected": selected,
        "current_alias": current.model_alias,
        "current_file": current.model_file,
    }


def entry_by_id(model_id: str, *, home: Path | None = None) -> CatalogEntry | None:
    """Look up one catalog id (any OrcaRouter model id works with the orca: prefix)."""
    if model_id.startswith(ORCA_PREFIX) and "/" in model_id:
        return orca_entry(model_id.removeprefix(ORCA_PREFIX))
    for entry in known_entries(home):
        if entry.id == model_id:
            return entry
    return None
