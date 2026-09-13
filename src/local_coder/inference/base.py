"""Adapter interface. One raw response per generation; no hidden calls."""
from __future__ import annotations

import abc

from .tokenizer import EstimateCounter, TokenCounter


class AdapterError(Exception):
    """Transport, timeout, or configuration failure. Consumes one slot."""


class AdapterUnavailable(AdapterError):
    """No model is configured or reachable. Never silently retried."""


class ModelAdapter(abc.ABC):
    """Returns raw model text. Parsing and validation belong to the controller."""

    name: str = "base"
    last_usage: dict | None = None
    last_request: dict | None = None

    @abc.abstractmethod
    def generate(self, briefing: str, step: int) -> str:
        """Return the raw response for generation slot `step`."""

    @abc.abstractmethod
    def provenance(self) -> dict[str, str]:
        """Adapter/model identity for runtime-owned records."""

    def token_counter(self) -> TokenCounter:
        """Counter for pre-flight prompt budgeting (estimates by default)."""
        return EstimateCounter()

    def context_limit(self) -> int | None:
        """Verified server context size, or None when unknown/unverified."""
        return None

    def server_info(self) -> dict:
        """Best-effort server metadata (never a model generation)."""
        return {}
