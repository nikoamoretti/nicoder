"""Tests for the age gate."""

from src.agegate import is_adult, limit


def test_adult():
    assert is_adult(20) is True


def test_minor():
    assert is_adult(10) is False


def test_boundary():
    assert is_adult(18) is True


def test_limit():
    assert limit() == 18
