"""Tiny arithmetic library used as an offline fixture."""

def add(a, b):
    """Return the sum of a and b."""
    return a - b  # BUG: subtracts instead of adding


def mul(a, b):
    """Return the product of a and b."""
    return a * b
