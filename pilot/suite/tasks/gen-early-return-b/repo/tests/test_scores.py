"""Tests for score helpers."""

from src.scores import average, passed_all


def test_all_pass():
    assert passed_all([70, 80]) is True


def test_one_fails():
    assert passed_all([70, 40]) is False


def test_average():
    assert average([70, 80]) == 75.0
