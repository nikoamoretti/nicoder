"""Tests for temperature conversion."""

from src.convert import celsius_to_fahrenheit, freezing_point


def test_boiling():
    assert celsius_to_fahrenheit(100) == 212.0


def test_freezing():
    assert celsius_to_fahrenheit(0) == 32.0


def test_constant():
    assert freezing_point() == 32.0
