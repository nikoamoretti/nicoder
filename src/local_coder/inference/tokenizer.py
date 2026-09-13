"""Token measurement: exact server counts when live, labeled estimates offline.

The controller budgets pre-flight with a counter and reserves space for the
chat template plus the generation budget. Post-flight, server-reported
`usage` replaces estimates with exact numbers. Estimates are always labeled
ESTIMATE at the call site; exact counts are labeled MEASURED.
"""
from __future__ import annotations

import abc


class TokenCounter(abc.ABC):
    """Counts prompt content tokens for budget enforcement."""

    name: str = "base"

    @property
    @abc.abstractmethod
    def exact(self) -> bool:
        """Whether counts are measured rather than estimated."""

    @abc.abstractmethod
    def count(self, text: str) -> int:
        """Count tokens in raw text (no chat template applied)."""


class EstimateCounter(TokenCounter):
    """Chars//4 heuristic. Always labeled ESTIMATE, never MEASURED."""

    name = "estimate"

    @property
    def exact(self) -> bool:
        return False

    def count(self, text: str) -> int:
        """Rough token estimate."""
        return max(1, len(text) // 4)


class LlamaCppCounter(TokenCounter):
    """Exact counts via the llama.cpp server /tokenize endpoint."""

    name = "llama.cpp-tokenize"

    def __init__(self, post_tokenize) -> None:
        """post_tokenize(text) performs POST /tokenize and returns token count."""
        self._post_tokenize = post_tokenize

    @property
    def exact(self) -> bool:
        return True

    def count(self, text: str) -> int:
        """Measured token count from the loaded model's own tokenizer."""
        return self._post_tokenize(text)
