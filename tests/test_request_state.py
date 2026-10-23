"""One definition of waiting / kept / discarded, shared by the snapshot, History, Keep and Discard."""
from __future__ import annotations

import pytest

from local_coder.general.request_state import (
    DISCARDED,
    KEPT,
    WAITING,
    built_on,
    is_closed,
    request_outcome,
    request_state,
    waiting_on_top,
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


def test_a_request_closed_by_a_later_keep_counts_as_kept():
    meta = {"kept": True, "kept_with": "req-2"}
    assert request_state(meta) == KEPT and is_closed(meta) is True
    assert request_outcome(meta) == "kept"


STACK = {
    "req-1": {},
    "req-2": {"built_on": "req-1"},
    "req-3": {"built_on": "req-2"},
    "req-other": {},
}


def test_the_requests_a_request_was_built_on_come_nearest_first():
    assert built_on(STACK, "req-3") == ["req-2", "req-1"]
    assert built_on(STACK, "req-2") == ["req-1"]
    assert built_on(STACK, "req-1") == []
    assert built_on(STACK, "req-other") == []
    assert built_on(STACK, "missing") == []


def test_a_chain_that_loops_or_points_nowhere_ends_instead_of_hanging():
    assert built_on({"a": {"built_on": "b"}, "b": {"built_on": "a"}}, "a") == ["b"]
    assert built_on({"a": {"built_on": "gone"}}, "a") == ["gone"]
    assert built_on({"a": {"built_on": 7}}, "a") == []
    assert built_on({"a": "not a dict"}, "a") == []


def test_only_undecided_requests_count_as_built_on_top_of_one():
    assert waiting_on_top(STACK, "req-1") == ["req-2", "req-3"]
    assert waiting_on_top(STACK, "req-2") == ["req-3"]
    assert waiting_on_top(STACK, "req-3") == []
    kept = {**STACK, "req-2": {"built_on": "req-1", "kept": True}}
    assert waiting_on_top(kept, "req-1") == ["req-3"], "a closed request is not waiting, but what is built on it is"
    decided = {
        **STACK,
        "req-2": {"built_on": "req-1", "discarded": True},
        "req-3": {"built_on": "req-2", "kept": True},
    }
    assert waiting_on_top(decided, "req-1") == []
