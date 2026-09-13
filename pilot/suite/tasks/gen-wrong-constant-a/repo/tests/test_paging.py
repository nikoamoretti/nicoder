"""Tests for API paging."""

from src.paging import DEFAULT_LIMIT, max_pages, page_params


def test_default_limit():
    assert page_params(2) == {"page": 2, "limit": 25}


def test_pages():
    assert max_pages(50) == 2


def test_constant_documented():
    assert DEFAULT_LIMIT == 25
