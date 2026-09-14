"""Shared adapter/backend construction for the CLI and the Mac app."""
from __future__ import annotations

import os
from pathlib import Path

from .config import RepoConfig
from .execution.base import ExecutionBackend
from .execution.isolated import IsolatedBackend, IsolationUnavailable
from .execution.trusted_local import TrustedLocalBackend
from .inference.base import ModelAdapter
from .inference.fake import ScriptedFakeAdapter, demo_script
from .inference.grammar import constraint_id, constraint_id_v2, constraint_id_v3
from .inference.llama_cpp import LlamaCppAdapter
from .machine import load_machine_config
from .persistence import EpisodeStore
from .profiles import DEFAULT_PROFILE, Profile, resolve_profile

DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "local-coder" / "episodes"


def episode_store(state_dir: Path | None = None) -> EpisodeStore:
    """Episode store; default is the per-user application directory."""
    raw = Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR
    return EpisodeStore(raw.expanduser().resolve())


def selected_profile(name: str | None) -> Profile | None:
    """Resolve a named profile; fake profiles and empty names skip this."""
    if not name or name.startswith("fake:"):
        return None
    if name == "llama":
        name = DEFAULT_PROFILE
    return resolve_profile(name)


def effective_protocol(profile: Profile | None, override: str | None = None) -> str:
    """Protocol string for this run."""
    if override:
        return override
    if profile is not None:
        return profile.protocol
    return "1"


def effective_verification(profile: Profile | None, override: str | None = None) -> str:
    """Verification policy for this run."""
    if override:
        return override
    if profile is not None:
        return profile.verification_policy
    return "off"


def effective_channel(profile: Profile | None, override: str | None = None) -> str:
    """Channel policy for this run."""
    if override:
        return override
    if profile is not None:
        return profile.channel_policy
    return "legacy"


def effective_thinking(profile: Profile | None, override: str | None = None) -> str:
    """Thinking expectation for this run."""
    if override:
        return override
    if profile is not None:
        return profile.thinking
    return "auto"


def build_llama_adapter(
    profile: Profile | None,
    *,
    endpoint: str | None = None,
    model: str | None = None,
    model_file: str | None = None,
    quantization: str | None = None,
    n_ctx: int | None = None,
    max_tokens: int | None = None,
    gen_timeout: int | None = None,
    protocol: str | None = None,
    channel: str | None = None,
    thinking: str | None = None,
) -> LlamaCppAdapter:
    """Build the live adapter from machine config, profile, and overrides."""
    cfg = load_machine_config()
    resolved_endpoint = os.environ.get("LOCAL_CODER_LLAMA_URL") or endpoint or cfg.endpoint
    protocol_id = effective_protocol(profile, protocol)
    constraint_map = {"1": constraint_id(), "2": constraint_id_v2(), "3": constraint_id_v3()}
    return LlamaCppAdapter(
        resolved_endpoint,
        model=model or cfg.model_alias,
        max_tokens=max_tokens or cfg.max_tokens,
        timeout_s=gen_timeout or cfg.gen_timeout,
        model_file=model_file or cfg.model_file or None,
        quantization=quantization or cfg.quantization,
        n_ctx=n_ctx or cfg.n_ctx,
        constraint=profile.constraint if profile else constraint_map[protocol_id],
        channel_policy=effective_channel(profile, channel),
        thinking=effective_thinking(profile, thinking),
    )


def build_adapter(profile_name: str | None, **kwargs: object) -> ModelAdapter:
    """Fake scripted adapter or the live llama.cpp adapter."""
    name = profile_name or ""
    if name.startswith("fake:"):
        return ScriptedFakeAdapter(demo_script(name), name)
    profile = selected_profile(profile_name)
    allowed = {
        "endpoint", "model", "model_file", "quantization", "n_ctx",
        "max_tokens", "gen_timeout", "protocol", "channel", "thinking",
    }
    options = {key: value for key, value in kwargs.items() if key in allowed and value is not None}
    return build_llama_adapter(profile, **options)  # type: ignore[arg-type]


def build_backend(config: RepoConfig, execution: str | None = None) -> ExecutionBackend:
    """Isolated backend by default; trusted-local only when chosen explicitly."""
    mode = execution or config.execution
    if mode == "trusted-local":
        return TrustedLocalBackend()
    backend = IsolatedBackend()
    backend.check_available()
    return backend


def isolation_status() -> dict[str, object]:
    """Whether the isolated worker can run. Never falls back to the host."""
    backend = IsolatedBackend()
    try:
        backend.check_available()
        return {"ok": True, "image": backend.image, "detail": "isolated worker is available"}
    except IsolationUnavailable as exc:
        return {"ok": False, "image": backend.image, "detail": str(exc)}
