"""The model library format (models.json in the bucket) and server settings per model family.

Each entry names a model, the bucket folder holding its files, the engine that
serves it, and that engine's arguments. `fallback_args` is tried when the first
launch fails (for example a speculative-decoding head the engine cannot load).
Standard library only: the GPU container imports this too.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

MANIFEST_NAME = "models.json"
MODELS_PREFIX = "models"
_ID = re.compile(r"[^a-z0-9.]+")

_QWEN_ARGS = [
    "--reasoning-parser", "qwen3",
    "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
    "--kv-cache-dtype", "fp8",
    "--max-model-len", "131072",
    "--max-num-seqs", "8",
]
_MTP = ["--speculative-config", '{"method":"mtp","num_speculative_tokens":3}']
PRESETS: dict[str, dict[str, Any]] = {
    "qwen3.8": {
        "args": [*_QWEN_ARGS, *_MTP],
        "fallback_args": list(_QWEN_ARGS),
        "context": 131072,
        "max_tokens": 32768,
        "temperature": 1.0,
        "top_p": 0.95,
    },
    "qwen3": {
        "args": list(_QWEN_ARGS),
        "context": 131072,
        "max_tokens": 32768,
        "temperature": 1.0,
        "top_p": 0.95,
    },
    "nemotron3.5": {
        "args": [
            "--trust-remote-code",
            "--reasoning-parser", "nemotron_v3",
            "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
            "--max-model-len", "131072",
            "--max-num-seqs", "8",
            "--speculative_config.method", "mtp", "--speculative_config.num_speculative_tokens", "3",
        ],
        "fallback_args": [
            "--trust-remote-code",
            "--reasoning-parser", "nemotron_v3",
            "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
            "--max-model-len", "131072",
            "--max-num-seqs", "8",
        ],
        "context": 131072,
        "max_tokens": 16384,
        "temperature": 1.0,
        "top_p": 0.95,
    },
    "generic": {
        "args": ["--enable-auto-tool-choice", "--tool-call-parser", "hermes", "--max-model-len", "32768"],
        "context": 32768,
        "max_tokens": 8192,
        "temperature": 0.7,
        "top_p": 0.95,
    },
}


def preset_for(repo: str) -> str:
    """The server settings that fit a Hugging Face repo name."""
    name = repo.lower()
    if "qwen3.8" in name or "qwen3.7" in name or "qwen3.6" in name or "qwen3.5" in name:
        return "qwen3.8"
    if "qwen3" in name:
        return "qwen3"
    if "nemotron-3.5" in name or "nemotron_3.5" in name:
        return "nemotron3.5"
    return "generic"


def model_id_for(repo: str) -> str:
    """A short, URL-safe id from a repo name: Qwen/Qwen3.8-27B-FP8 -> qwen3.8-27b-fp8."""
    tail = repo.rsplit("/", 1)[-1].lower()
    return _ID.sub("-", tail).strip("-.") or "model"


def display_name(repo: str) -> str:
    """A readable name: Qwen/Qwen3.8-27B-FP8 -> Qwen3.8 27B FP8."""
    return repo.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")


def make_entry(
    repo: str,
    *,
    preset: str | None = None,
    model_id: str | None = None,
    name: str | None = None,
    size_bytes: int | None = None,
    revision: str = "main",
) -> dict[str, Any]:
    """A library entry for a repo, with the preset's server settings."""
    chosen = preset or preset_for(repo)
    if chosen not in PRESETS:
        raise ValueError(f"unknown preset {chosen!r}; choose from {', '.join(sorted(PRESETS))}")
    settings = PRESETS[chosen]
    ident = model_id or model_id_for(repo)
    entry: dict[str, Any] = {
        "id": ident,
        "name": name or display_name(repo),
        "repo": repo,
        "revision": revision,
        "dir": ident,
        "engine": "vllm",
        "preset": chosen,
        "args": list(settings["args"]),
        "context": settings["context"],
        "max_tokens": settings["max_tokens"],
        "temperature": settings["temperature"],
        "top_p": settings["top_p"],
        "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if "fallback_args" in settings:
        entry["fallback_args"] = list(settings["fallback_args"])
    if size_bytes is not None:
        entry["size_bytes"] = int(size_bytes)
    return entry


def empty() -> dict[str, Any]:
    """A library with no models."""
    return {"version": 1, "models": []}


def parse(text: str | bytes | None) -> dict[str, Any]:
    """Read models.json; a missing or broken file is an empty library."""
    if not text:
        return empty()
    try:
        data = json.loads(text)
    except ValueError:
        return empty()
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return empty()
    data["models"] = [row for row in data["models"] if isinstance(row, dict) and row.get("id") and row.get("dir")]
    return data


def upsert(library: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Add an entry, replacing one with the same id."""
    models = [row for row in library.get("models", []) if row.get("id") != entry["id"]]
    models.append(entry)
    return {**library, "version": 1, "models": sorted(models, key=lambda row: str(row["id"]))}


def remove(library: dict[str, Any], model_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Drop an entry; returns the new library and the removed entry (None when absent)."""
    kept: list[dict[str, Any]] = []
    gone: dict[str, Any] | None = None
    for row in library.get("models", []):
        if row.get("id") == model_id and gone is None:
            gone = row
        else:
            kept.append(row)
    return {**library, "models": kept}, gone


def find(library: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    """The entry with this id."""
    return next((row for row in library.get("models", []) if row.get("id") == model_id), None)


def dumps(library: dict[str, Any]) -> str:
    """models.json text."""
    return json.dumps(library, indent=2) + "\n"
