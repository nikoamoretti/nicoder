"""Benjamini-Hochberg correction for batches of change-detection tests."""

from __future__ import annotations

from collections.abc import Sequence


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Adjusted q-values for a batch of p-values (B-H FDR correction)."""
    n = len(p_values)
    if n == 0:
        return []
    ordered = sorted((p, i) for i, p in enumerate(p_values))
    q_values = [0.0] * n
    running = 1.0
    for rank, (p, original_index) in enumerate(reversed(ordered), start=1):
        adjusted = p * n / rank
        running = min(running, adjusted)
        q_values[original_index] = min(1.0, running)
    return q_values
