"""Data-origin classification for suite runs.

Separates real provider executions from mock, synthetic, demo, and test data so
that baseline statistics and change detection are never contaminated.
"""

from __future__ import annotations

from typing import Literal

DataOrigin = Literal["live", "mock", "synthetic", "demo", "test"]

DATA_ORIGINS: tuple[DataOrigin, ...] = ("live", "mock", "synthetic", "demo", "test")
LIVE_ORIGIN: DataOrigin = "live"

# Origins that represent real provider executions.
REAL_ORIGINS: tuple[DataOrigin, ...] = ("live",)


def derive_origin(
    provider: str | None,
    suite_name: str | None,
    explicit: DataOrigin | None = None,
) -> DataOrigin:
    """Derive a run's data origin.

    An explicit value wins. Otherwise: demo-prefixed suites are ``demo``,
    mock-provider runs are ``mock``, openai (live) runs are ``live``, and
    everything else falls back to ``test``.
    """
    if explicit is not None:
        if explicit not in DATA_ORIGINS:
            raise ValueError(f"unknown data origin {explicit!r}; expected one of {DATA_ORIGINS}")
        return explicit
    if suite_name and suite_name.lower().startswith("demo"):
        return "demo"
    if provider == "mock":
        return "mock"
    if provider == "openai":
        return "live"
    return "test"
