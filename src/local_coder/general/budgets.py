"""User-visible request ceilings. Distinct from OpenCode tool-call counts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RequestBudget:
    """One actual budget policy for a user request."""

    kind: str
    wall_seconds: int
    max_generations: int
    max_commands: int
    max_output_bytes: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "kind": self.kind,
            "wall_seconds": self.wall_seconds,
            "max_generations": self.max_generations,
            "max_commands": self.max_commands,
            "max_output_bytes": self.max_output_bytes,
        }


CREATE = RequestBudget(
    kind="create",
    wall_seconds=30 * 60,
    max_generations=200,
    max_commands=200,
    max_output_bytes=2_000_000,
)
FOLLOW_UP = RequestBudget(
    kind="follow_up",
    wall_seconds=30 * 60,
    max_generations=150,
    max_commands=120,
    max_output_bytes=1_000_000,
)


def budget_for(kind: str) -> RequestBudget:
    """Return the create or follow-up ceiling."""
    return FOLLOW_UP if kind == "follow_up" else CREATE
