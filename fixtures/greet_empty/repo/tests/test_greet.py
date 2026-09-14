"""Acceptance tests for greet()."""

from src.greet import greet


def test_named():
    assert greet("Ada") == "Hello, Ada!"


def test_empty():
    assert greet("") == "Hello!"
