"""One definition of waiting / kept / discarded, shared by the snapshot, History, Keep and Discard."""
from __future__ import annotations

import pytest

from local_coder.general.request_state import (
    DISCARDED,
    KEPT,
    WAITING,
    is_closed,
    request_outcome,
    request_state,
)


@pytest.mark.parametrize(
    ("meta", "state"),
    [
        ({}, WAITING),
        ({"kept": False, "discarded": False}, WAITING),
        ({"kept": True}, KEPT),
        ({"discarded": True}, DISCARDED),
        # What the old "Discard after Keep" bug left behind: the change is in the project, so it counts as kept.
        ({"kept": True, "discarded": True}, KEPT),
        (None, WAITING),
        ("not a dict", WAITING),
    ],
)
def test_state_comes_from_the_flags_and_kept_wins(meta, state):
    assert request_state(meta) == state


def test_only_kept_and_discarded_requests_are_closed():
    assert is_closed({"kept": True}) is True
    assert is_closed({"discarded": True}) is True
    assert is_closed({}) is False


@pytest.mark.parametrize(
    ("meta", "outcome"),
    [
        ({}, "review"),
        ({"kept": True}, "kept"),
        ({"discarded": True}, "discarded"),
        ({"kept": True, "discarded": True}, "kept"),
        ({"agent": {"status": "stopped"}}, "stopped"),
        ({"agent": {"status": "time_limit"}}, "time_limit"),
        ({"agent": {"status": "answered"}}, "review"),
        # A decision beats how the run ended.
        ({"agent": {"status": "stopped"}, "discarded": True}, "discarded"),
        ({"agent": "oops"}, "review"),
    ],
)
def test_outcome_is_the_word_history_shows(meta, outcome):
    assert request_outcome(meta) == outcome
