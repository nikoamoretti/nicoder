"""Tests for upload retries."""

from src.upload import MAX_RETRIES, may_retry, retries_left


def test_remaining():
    assert retries_left(1) == 2


def test_allowed():
    assert may_retry(2) is True


def test_constant_documented():
    assert MAX_RETRIES == 3
