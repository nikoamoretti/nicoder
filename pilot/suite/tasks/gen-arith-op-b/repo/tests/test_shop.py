"""Tests for shop discounts."""

from src.shop import apply_discount, loyalty_price


def test_half_off():
    assert apply_discount(80.0, 0.5) == 40.0


def test_no_discount():
    assert apply_discount(80.0, 0.0) == 80.0


def test_loyalty():
    assert loyalty_price(100.0) == 95.0
