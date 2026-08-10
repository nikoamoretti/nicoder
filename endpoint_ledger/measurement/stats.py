"""Statistical helpers for honest measurement presentation.

No false precision: every binary rate is displayed with its sample count and a
95% Wilson confidence interval so small samples show large uncertainty.
"""

from __future__ import annotations

import math

WILSON_Z = 1.96


def wilson_interval(
    successes: int,
    total: int,
    z: float = WILSON_Z,
) -> tuple[float, float] | None:
    """Wilson score interval for a binary proportion.

    Returns ``(lower, upper)`` as proportions in [0, 1], or None when there are
    no observations.
    """
    if total <= 0:
        return None
    if successes < 0 or successes > total:
        raise ValueError(f"successes {successes} outside [0, {total}]")
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    half_width = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total) / denominator
    lower = max(0.0, centre - half_width)
    upper = min(1.0, centre + half_width)
    return round(lower, 4), round(upper, 4)


def format_interval(interval: tuple[float, float] | None) -> str:
    """Human-readable interval that makes small samples look uncertain."""
    if interval is None:
        return "n/a"
    lower, upper = interval
    return f"{lower:.1%}–{upper:.1%}"
