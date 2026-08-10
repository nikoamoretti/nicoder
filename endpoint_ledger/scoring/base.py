"""Scoring framework.

Scorers are deterministic pure functions of (provider response, probe version).
They never call an LLM and never guess. Every scorer exposes a stable ``name``,
a ``version``, a single ``metric_name``, and returns a structured
``ScoreResult`` carrying its evidence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from endpoint_ledger.runner.canonical_response import CanonicalProviderResponse


@dataclass
class ScoreResult:
    scorer_name: str
    scorer_version: str
    metric_name: str
    passed: bool | None
    explanation: str
    numeric_value: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    # Measurement role: primary / diagnostic / informational.
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Scorer(ABC):
    name: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    metric_name: ClassVar[str]

    @abstractmethod
    def score(
        self,
        response: CanonicalProviderResponse,
        expected_behavior: dict[str, Any],
    ) -> ScoreResult:
        """Score one observation against the probe's expected behavior."""


class ScoreRegistry:
    def __init__(self) -> None:
        self._scorers: dict[str, Scorer] = {}

    def register(self, scorer: Scorer) -> None:
        self._scorers[scorer.name] = scorer

    def get(self, name: str) -> Scorer | None:
        return self._scorers.get(name)

    def names(self) -> list[str]:
        return sorted(self._scorers)

    def __contains__(self, name: str) -> bool:
        return name in self._scorers

    def unknown(self, names: list[str]) -> list[str]:
        return [n for n in names if n not in self._scorers]
