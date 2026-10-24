"""One definition of waiting / kept / discarded, shared by the snapshot, History, Keep and Discard."""
from __future__ import annotations

import pytest

from local_coder.general.request_state import (
    DISCARDED,
    KEPT,
    WAITING,
    built_on,
    dropped_files,
    is_closed,
    replaced_sentence,
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


def test_a_kept_request_a_later_keep_took_files_back_from_is_replaced():
    """Net-out: only a kept request with files in its `dropped` list reads `replaced`; the list means nothing else."""
    assert request_outcome({"kept": True, "kept_with": "req-2", "dropped": ["src/gen.py"]}) == "replaced"
    assert request_state({"kept": True, "dropped": ["src/gen.py"]}) == KEPT
    assert is_closed({"kept": True, "dropped": ["src/gen.py"]}) is True
    assert request_outcome({"kept": True, "kept_with": "req-2", "dropped": []}) == "kept"
    assert request_outcome({"kept": True, "kept_with": "req-2"}) == "kept"
    assert request_outcome({"dropped": ["src/gen.py"]}) == "review", "a request that is not kept is not replaced"
    assert request_outcome({"discarded": True, "dropped": ["src/gen.py"]}) == "discarded"
    assert request_outcome({"kept": True, "dropped": "src/gen.py"}) == "kept", "a damaged list counts as none"


def test_the_dropped_files_are_a_sorted_list_of_names_and_a_damaged_record_has_none():
    assert dropped_files({"dropped": ["src/b.py", "src/a.py", "src/b.py"]}) == ["src/a.py", "src/b.py"]
    assert dropped_files({"dropped": ["src/a.py", 7, None]}) == ["src/a.py"]
    assert dropped_files({}) == [] and dropped_files({"dropped": "src/a.py"}) == []
    assert dropped_files(None) == [] and dropped_files("not a dict") == []


@pytest.mark.parametrize(
    ("files", "named"),
    [
        (["src/gen.py"], "src/gen.py"),
        (["src/b.py", "src/a.py"], "src/a.py or src/b.py"),
        (["c.py", "a.py", "b.py"], "a.py, b.py or c.py"),
        (["d.py", "b.py", "a.py", "c.py"], "a.py, b.py, c.py or 1 more file"),
        (["e.py", "d.py", "c.py", "b.py", "a.py"], "a.py, b.py, c.py or 2 more files"),
        (["b.py", "a.py", "b.py", "c.py", "a.py", "c.py"], "a.py, b.py or c.py"),
        (["z.py", "y.py", "x.py", "w.py", "x.py", "v.py", "w.py"], "v.py, w.py, x.py or 2 more files"),
    ],
    ids=["one", "two", "three", "four", "five", "duplicates", "more duplicates"],
)
def test_the_replaced_sentence_names_at_most_three_files_sorted_and_counts_the_rest(files, named):
    """A pure function of the file list: sorted, each file once, whatever order or repeats it is given."""
    assert replaced_sentence(files) == (
        f"These changes were replaced by a later request you kept. Nothing from them went into {named}."
    )
    assert replaced_sentence(reversed(files)) == replaced_sentence(files)
    assert replaced_sentence(iter(files)) == replaced_sentence(files), "any iterable"


def test_the_replaced_sentence_needs_at_least_one_file():
    with pytest.raises(ValueError):
        replaced_sentence([])


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
