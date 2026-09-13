"""Tests for list helpers."""

from src.lists import first_or, is_empty


def test_empty():
    assert is_empty([]) is True


def test_full():
    assert is_empty([1]) is False


def test_fallback():
    assert first_or([], "x") == "x"
