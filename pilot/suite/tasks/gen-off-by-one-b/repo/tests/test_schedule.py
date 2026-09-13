"""Tests for schedule helpers."""

from src.schedule import midnight, readings_between


def test_span():
    assert readings_between(9, 12) == [9, 10, 11, 12]


def test_single():
    assert readings_between(9, 9) == [9]


def test_midnight():
    assert midnight() == 0
