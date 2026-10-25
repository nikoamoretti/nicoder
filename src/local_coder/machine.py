"""Machine-local configuration: model paths, endpoint, server binary.

Stored at ~/.config/local-coder/config.toml (or LOCAL_CODER_CONFIG_DIR).
Never committed to the repository; never contains secrets. Paths here are
machine-specific by design, which is why they do not live in the checkout.
A hosted model stores only the provider, its endpoint, and the *name* of
the API key; the key itself is read at request time (see hosted.py).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

CONFIG_DIR_ENV = "LOCAL_CODER_CONFIG_DIR"
DEFAULT_MODEL = Path.home() / ".cache" / "local-coder" / "models" / "Qwen_Qwen3.5-4B-Q4_K_M.gguf"
DEFAULT_MODEL_ALIAS = "Qwen3.5-4B-Q4_K_M"
DEFAULT_BONSAI = (
    Path.home() / "Models" / "Bonsai-demo" / "models" / "bonsai2-gguf" / "27B"
    / "Ternary-Bonsai-2-27B-PQ2_0.gguf"
)
DEFAULT_BONSAI_ALIAS = "Ternary-Bonsai-2-27B-PQ2_0"
DEFAULT_BONSAI_SERVER = Path.home() / "Models" / "Bonsai-demo" / "bin" / "mac" / "llama-server"
DEFAULT_ENDPOINT = "http://127.0.0.1:8080"
DEFAULT_SERVER_BINARY = "llama-server"


class MachineConfig(BaseModel):
    """Machine-local configuration; all fields optional with sane defaults."""

    model_config = ConfigDict(extra="forbid")
    model_file: str = ""
    model_alias: str = DEFAULT_MODEL_ALIAS
    endpoint: str = DEFAULT_ENDPOINT
    server_binary: str = DEFAULT_SERVER_BINARY
    quantization: str = "Q4_K_M"
    n_ctx: int = 8192
    max_tokens: int = 2048
    gen_timeout: int = 300
    provider: str = "local"
    api_key_name: str = ""
    temperature: float = 0.0
    top_p: float = 0.0
    reasoning: str = "on"
    reasoning_switch: bool = False


def is_hosted(cfg: MachineConfig) -> bool:
    """True when the selected model runs on a provider's servers, not on llama.cpp here."""
    return cfg.provider != "local"


def config_dir() -> Path:
    """Machine-local config directory (overridable for tests)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "local-coder"


def config_path() -> Path:
    """Path to the machine-local TOML file."""
    return config_dir() / "config.toml"


def _guess_installed_model() -> MachineConfig:
    """Prefer Bonsai when the GGUF and PrismML server are already on this Mac.

    Tests that override the config dir must not inherit this machine's models.
    """
    if not os.environ.get(CONFIG_DIR_ENV):
        if DEFAULT_BONSAI.is_file() and DEFAULT_BONSAI_SERVER.is_file():
            return MachineConfig(
                model_file=str(DEFAULT_BONSAI),
                model_alias=DEFAULT_BONSAI_ALIAS,
                server_binary=str(DEFAULT_BONSAI_SERVER),
                quantization="PQ2_0",
                n_ctx=8192,
                max_tokens=3072,
                gen_timeout=600,
            )
    cfg = MachineConfig()
    if DEFAULT_MODEL.exists():
        return cfg.model_copy(update={"model_file": str(DEFAULT_MODEL)})
    return cfg


def load_machine_config() -> MachineConfig:
    """Load machine config; missing file yields defaults (model path guessed)."""
    path = config_path()
    if not path.exists():
        return _guess_installed_model()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    return MachineConfig.model_validate(data)


def write_machine_config(cfg: MachineConfig) -> Path:
    """Write the machine-local config atomically. Returns its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Nicoder machine-local configuration (not committed, no secrets).",
        f'model_file = "{cfg.model_file}"',
        f'model_alias = "{cfg.model_alias}"',
        f'endpoint = "{cfg.endpoint}"',
        f'server_binary = "{cfg.server_binary}"',
        f'quantization = "{cfg.quantization}"',
        f"n_ctx = {cfg.n_ctx}",
        f"max_tokens = {cfg.max_tokens}",
        f"gen_timeout = {cfg.gen_timeout}",
        f'provider = "{cfg.provider}"',
        f'api_key_name = "{cfg.api_key_name}"',
        f"temperature = {float(cfg.temperature)}",
        f"top_p = {float(cfg.top_p)}",
        f'reasoning = "{cfg.reasoning}"',
        f"reasoning_switch = {'true' if cfg.reasoning_switch else 'false'}",
        "",
    ]
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)
    return path


def machine_status(cfg: MachineConfig) -> list[tuple[str, bool, str]]:
    """(name, ok, detail) prerequisite facts; no model generations."""
    import shutil

    if is_hosted(cfg):
        from .hosted import credential_status, provider_place

        ok, where = credential_status(cfg)
        return [
            ("model", True, f"{cfg.model_alias} on {provider_place(cfg)} (nothing loads on this Mac)"),
            ("API key", ok, where),
            ("endpoint", True, cfg.endpoint),
        ]
    rows: list[tuple[str, bool, str]] = []
    model = Path(cfg.model_file).expanduser() if cfg.model_file else None
    if model is None:
        rows.append(("model file", False, "not configured (run: local-coder setup)"))
    elif model.exists():
        size_mb = round(model.stat().st_size / (1024 * 1024), 1)
        rows.append(("model file", True, f"{model} ({size_mb} MB on disk)"))
    else:
        rows.append(("model file", False, f"missing: {model}"))
    binary = shutil.which(cfg.server_binary) or (
        cfg.server_binary if Path(cfg.server_binary).exists() else None)
    rows.append(("llama-server binary", bool(binary), binary or f"not found: {cfg.server_binary}"))
    rows.append(("endpoint", True, cfg.endpoint))
    return rows
