"""Tests for client configuration."""

from src.client import default_timeout, get_timeout


def test_explicit():
    assert get_timeout({"timeout": 5}) == 5


def test_missing_uses_default():
    assert get_timeout({}) == 30


def test_default():
    assert default_timeout() == 30
