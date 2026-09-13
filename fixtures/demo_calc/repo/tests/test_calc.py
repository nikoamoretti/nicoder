"""Acceptance tests for the demo calculator fixture."""

from src.calc import add, mul


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, -1) == -2


def test_mul():
    assert mul(2, 3) == 6
