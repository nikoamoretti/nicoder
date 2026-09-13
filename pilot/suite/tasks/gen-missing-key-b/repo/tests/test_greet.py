"""Tests for greetings."""

from src.greet import formal, welcome


def test_nickname():
    assert welcome({"name": "Ada L", "nickname": "ada"}) == "hi ada"


def test_fallback():
    assert welcome({"name": "Ada L"}) == "hi Ada L"


def test_formal():
    assert formal({"name": "Ada L"}) == "hello Ada L"
