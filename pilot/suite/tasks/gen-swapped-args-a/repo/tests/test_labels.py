"""Tests for mailing labels."""

from src.labels import make_label, make_tag


def test_label():
    assert make_label("ada", "X1") == "X1-ada"


def test_tag():
    assert make_tag("X1") == "[X1]"
