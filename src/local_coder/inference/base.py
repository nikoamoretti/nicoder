"""Adapter interface. One raw response per generation; no hidden calls."""
from __future__ import annotations

import abc


class AdapterError(Exception):
    """Transport, timeout, or configuration failure. Consumes one slot."""


class AdapterUnavailable(AdapterError):
    """No model is configured or reachable. Never silently retried."""


class ModelAdapter(abc.ABC):
    """Returns raw model text. Parsing and validation belong to the controller."""

    name: str = "base"

    @abc.abstractmethod
    def generate(self, briefing: str, step: int) -> str:
        """Return the raw response for generation slot `step`."""

    @abc.abstractmethod
    def provenance(self) -> dict[str, str]:
        """Adapter/model identity for runtime-owned records."""
