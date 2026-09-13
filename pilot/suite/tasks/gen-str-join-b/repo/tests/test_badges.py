"""Tests for badges."""

from src.badges import initials, monogram


def test_initials():
    assert initials("Ada", "Lovelace") == "AL"


def test_monogram():
    assert monogram("Ada", "B", "Lovelace") == "ABL"
