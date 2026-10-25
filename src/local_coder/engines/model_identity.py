"""Authoritative local model identity. No silent fallback to another model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_coder.machine import MachineConfig, load_machine_config
from local_coder.server import server_status

from .opencode_config import PROVIDER_ID


class ModelIdentityError(Exception):
    """Requested, configured, and served identities do not match."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = "model_mismatch"


@dataclass(frozen=True)
class ModelIdentity:
    """Requested vs observed names. Aliases are compared explicitly."""

    requested: str
    configured: str
    served: str | None
    provider: str = PROVIDER_ID

    def as_dict(self) -> dict[str, str | None]:
        return {
            "requested": self.requested,
            "configured": self.configured,
            "served": self.served,
            "provider": self.provider,
        }


def resolve_model_id(explicit: str | None = None, cfg: MachineConfig | None = None) -> str:
    """Return the single model id this process is allowed to use."""
    machine = cfg or load_machine_config()
    if explicit and machine.model_alias and explicit != machine.model_alias:
        raise ModelIdentityError(
            f"requested model {explicit!r} does not match configured "
            f"{machine.model_alias!r}. Refusing to pick another model."
        )
    return explicit or machine.model_alias


def inspect_identity(requested: str, cfg: MachineConfig | None = None) -> ModelIdentity:
    """Read configured and served names without generating tokens."""
    machine = cfg or load_machine_config()
    status = server_status(machine)
    served = status.get("model_alias")
    served_name = str(served) if served else None
    return ModelIdentity(
        requested=requested,
        configured=machine.model_alias,
        served=served_name,
    )


def assert_identity(requested: str, cfg: MachineConfig | None = None) -> ModelIdentity:
    """Fail before a coding task if llama.cpp is serving a different alias."""
    identity = inspect_identity(requested, cfg)
    if identity.configured != requested:
        raise ModelIdentityError(
            f"configured alias {identity.configured!r} is not {requested!r}"
        )
    if identity.served and identity.served != requested:
        raise ModelIdentityError(
            f"llama.cpp is serving {identity.served!r}, not the requested "
            f"{requested!r}. Stop that server or load the configured model. "
            "Nicoder will not switch models silently."
        )
    return identity


def stale_mapping(registered: Any, expected: str) -> bool:
    """True when an OpenCode provider table still lists only an old alias."""
    if registered == expected:
        return False
    if isinstance(registered, str):
        return not registered.endswith(expected)
    if isinstance(registered, dict):
        models = registered.get("models") or registered
        if isinstance(models, dict):
            return expected not in models and not any(
                str(k).endswith(expected) for k in models
            )
        if isinstance(models, list):
            return expected not in [str(item) for item in models]
    return True
