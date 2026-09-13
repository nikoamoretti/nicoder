"""Tests for cart helpers."""

from src.cart import cart_count, cart_total


def test_total():
    assert cart_total([4, 5, 6]) == 15


def test_empty():
    assert cart_total([]) == 0


def test_count():
    assert cart_count([4, 5, 6]) == 3
