"""Acceptance tests for the counter fixture."""

from src.counter import Counter


def test_increment():
    counter = Counter()
    assert counter.increment() == 1


def test_increment_twice():
    counter = Counter()
    counter.increment()
    assert counter.increment() == 2


def test_reset():
    counter = Counter()
    counter.increment()
    assert counter.reset() == 0
