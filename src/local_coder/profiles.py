"""Frozen named inference profiles for the experimental v1 CLI.

Profiles bundle the verified model behavior into reproducible identities.
They are never switched automatically after a failed task: the run records
the profile name in episode provenance, resume refuses a mismatched
profile, and the user selects d1 explicitly if they want the fallback.
"""
from __future__ import annotations

from dataclasses import dataclass

from .inference.grammar import constraint_id_v2, constraint_id_v3
from .inference.llama_cpp import TRANSPORT_POLICY
from .spans import SPAN_CATALOGUE_VERSION


@dataclass(frozen=True)
class Profile:
    """One named, versioned inference configuration."""

    name: str
    label: str
    protocol: str
    constraint: str
    catalogue: str
    verification_policy: str
    channel_policy: str
    thinking: str


D2 = Profile(
    name="d2",
    label="span-edit (default, experimental)",
    protocol="3",
    constraint=constraint_id_v3(),
    catalogue=SPAN_CATALOGUE_VERSION,
    verification_policy="mandatory",
    channel_policy="final-only",
    thinking="off",
)

D1 = Profile(
    name="d1",
    label="exact-text replacement (explicit fallback)",
    protocol="2",
    constraint=constraint_id_v2(),
    catalogue="none",
    verification_policy="mandatory",
    channel_policy="final-only",
    thinking="off",
)

PROFILES: dict[str, Profile] = {profile.name: profile for profile in (D2, D1)}
DEFAULT_PROFILE = D2.name


def resolve_profile(name: str) -> Profile:
    """Resolve a named profile; unknown names are refused, never guessed."""
    profile = PROFILES.get(name)
    if profile is None:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown profile {name!r}; known profiles: {known} (or fake:<name> for tests)")
    return profile


def profile_identity(profile: Profile) -> dict[str, str]:
    """Complete recorded identity for episode provenance and reports."""
    return {
        "profile_name": profile.name,
        "profile_label": profile.label,
        "protocol_version": profile.protocol,
        "constraint": profile.constraint,
        "catalogue": profile.catalogue,
        "verification_policy": profile.verification_policy,
        "channel_policy": profile.channel_policy,
        "thinking": profile.thinking,
        "transport_policy": TRANSPORT_POLICY,
    }
