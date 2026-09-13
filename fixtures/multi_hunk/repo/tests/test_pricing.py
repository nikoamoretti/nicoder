"""Acceptance tests for order pricing."""

from src.pricing import grand_total, line_total, with_tax


def test_line_total():
    assert line_total(3, 7.0) == 21.0


def test_grand_total_discount():
    assert grand_total([(2, 10.0), (1, 5.0)], discount=0.2) == 20.0


def test_with_tax():
    assert with_tax(100.0) == 120.0
