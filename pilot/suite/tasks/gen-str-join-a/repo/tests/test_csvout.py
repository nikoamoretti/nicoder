"""Tests for CSV helpers."""

from src.csvout import csv_header, csv_line


def test_line():
    assert csv_line(["a", "b"]) == "a,b"


def test_single():
    assert csv_line(["a"]) == "a"


def test_header():
    assert csv_header(["a", "b"]) == "a,b"
