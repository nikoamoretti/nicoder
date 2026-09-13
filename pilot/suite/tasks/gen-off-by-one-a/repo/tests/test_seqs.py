"""Tests for sequence helpers."""

from src.seqs import first_n, last


def test_first_two():
    assert first_n([1, 2, 3], 2) == [1, 2]


def test_first_zero():
    assert first_n([1, 2, 3], 0) == []


def test_last():
    assert last([1, 2, 3]) == 3
