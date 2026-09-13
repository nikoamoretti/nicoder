"""Tests for canvas movement."""

from src.canvas import move_point, origin


def test_move():
    assert move_point(3, 4, 1, 2) == (4, 6)


def test_still():
    assert move_point(3, 4, 0, 0) == (3, 4)


def test_origin():
    assert origin() == (0, 0)
