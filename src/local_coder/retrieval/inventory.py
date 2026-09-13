"""Bounded inventory of permitted source and test paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..paths import build_inventory


@dataclass
class Inventory:
    """Permitted retrievable paths for one episode."""

    repo: Path
    files: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        repo: Path,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
    ) -> Inventory:
        """Build the inventory from the live filesystem under a policy."""
        return cls(repo=repo, files=build_inventory(repo, allow=allow, deny=deny))

    def python_files(self) -> list[str]:
        """Python sources first for symbol indexing."""
        return [f for f in self.files if f.endswith(".py")]
