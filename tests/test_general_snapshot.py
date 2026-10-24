"""B/C/U snapshot acceptance: current tree, conflicts, unrelated edits."""
from __future__ import annotations

from pathlib import Path

import pytest

from local_coder.general import snapshot as snap
from local_coder.general.snapshot import apply_delta, compute_delta, copy_snapshot, find_conflicts, index_tree


def _write(root: Path, rel: str, text: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")


def test_snapshot_includes_untracked_and_skips_node_modules(tmp_path):
    src = tmp_path / "user"
    _write(src, "src/App.tsx", "export default function App() { return null }\n")
    _write(src, "notes.txt", "untracked note\n")
    _write(src, "node_modules/pkg/index.js", "secret-dep\n")
    _write(src, "package-lock.json", "{}\n")
    _write(src, ".env", "TOKEN=1\n")
    dest = tmp_path / "snap"
    index = copy_snapshot(src, dest)
    assert "src/App.tsx" in index.files
    assert "notes.txt" in index.files
    assert "node_modules/pkg/index.js" not in index.files
    assert "package-lock.json" not in index.files
    assert ".env" not in index.files
    assert not (dest / "node_modules").exists()


def test_read_index_survives_later_hardlink_writes(tmp_path):
    from local_coder.general.snapshot import clone_snapshot, read_index, write_index

    src = tmp_path / "user"
    _write(src, "src/App.tsx", "one\n")
    baseline = tmp_path / "B"
    live = tmp_path / "live"
    copy_snapshot(src, baseline)
    clone_snapshot(baseline, live)
    write_index(tmp_path / "baseline.json", index_tree(baseline))
    _write(live, "src/App.tsx", "two\n")
    frozen = read_index(tmp_path / "baseline.json")
    live_index = index_tree(live)
    delta = compute_delta(frozen, live_index)
    assert "src/App.tsx" in delta.touched()


def test_clone_snapshot_duplicates_an_existing_snapshot(tmp_path):
    from local_coder.general.snapshot import clone_snapshot

    src = tmp_path / "user"
    _write(src, "src/App.tsx", "export {}\n")
    first = tmp_path / "a"
    second = tmp_path / "b"
    copy_snapshot(src, first)
    index = clone_snapshot(first, second)
    assert "src/App.tsx" in index.files
    assert (second / "src" / "App.tsx").read_text(encoding="utf-8") == "export {}\n"


def test_keep_applies_delta_and_preserves_unrelated_user_edit(tmp_path):
    user = tmp_path / "user"
    _write(user, "src/App.tsx", "one\n")
    _write(user, "README.md", "hello\n")
    baseline = tmp_path / "B"
    candidate = tmp_path / "C"
    copy_snapshot(user, baseline)
    copy_snapshot(user, candidate)
    _write(candidate, "src/App.tsx", "two\n")
    _write(candidate, "src/board.tsx", "board\n")
    _write(user, "README.md", "hello from me\n")
    result = apply_delta(user, baseline, candidate)
    assert result["ok"] is True
    assert (user / "src/App.tsx").read_text(encoding="utf-8") == "two\n"
    assert (user / "src/board.tsx").read_text(encoding="utf-8") == "board\n"
    assert (user / "README.md").read_text(encoding="utf-8") == "hello from me\n"


def test_keep_refuses_conflict_when_user_changed_touched_file(tmp_path):
    user = tmp_path / "user"
    _write(user, "src/App.tsx", "one\n")
    baseline = tmp_path / "B"
    candidate = tmp_path / "C"
    copy_snapshot(user, baseline)
    copy_snapshot(user, candidate)
    _write(candidate, "src/App.tsx", "engine\n")
    _write(user, "src/App.tsx", "user\n")
    result = apply_delta(user, baseline, candidate)
    assert result["ok"] is False
    assert result["code"] == "conflict"
    assert (user / "src/App.tsx").read_text(encoding="utf-8") == "user\n"


def test_rename_detected_by_content_identity(tmp_path):
    before = tmp_path / "B"
    after = tmp_path / "C"
    _write(before, "old.ts", "same-bytes\n")
    _write(after, "new.ts", "same-bytes\n")
    delta = compute_delta(index_tree(before), index_tree(after))
    assert delta.renamed == [("old.ts", "new.ts")]
    assert not delta.added
    assert not delta.deleted


def test_new_candidate_path_that_exists_in_user_tree_is_conflict(tmp_path):
    user = tmp_path / "user"
    _write(user, "keep.txt", "x\n")
    baseline = tmp_path / "B"
    candidate = tmp_path / "C"
    copy_snapshot(user, baseline)
    copy_snapshot(user, candidate)
    _write(candidate, "fresh.ts", "engine\n")
    _write(user, "fresh.ts", "user\n")
    conflicts = find_conflicts(index_tree(baseline), index_tree(candidate), index_tree(user))
    assert any(row.path == "fresh.ts" for row in conflicts)


def test_sync_source_tree_preserves_node_modules(tmp_path):
    from local_coder.general.snapshot import sync_source_tree

    source = tmp_path / "live"
    dest = tmp_path / "candidate"
    _write(source, "src/App.tsx", "updated\n")
    _write(dest, "src/App.tsx", "old\n")
    _write(dest, "src/gone.tsx", "remove me\n")
    node = dest / "node_modules" / "pkg" / "index.js"
    node.parent.mkdir(parents=True)
    node.write_text("keep-dep\n", encoding="utf-8")
    index = sync_source_tree(source, dest)
    assert (dest / "src/App.tsx").read_text(encoding="utf-8") == "updated\n"
    assert not (dest / "src/gone.tsx").exists()
    assert node.read_text(encoding="utf-8") == "keep-dep\n"
    assert "node_modules/pkg/index.js" not in index.files


# --- The refusal sentence --------------------------------------------------------------------------------------

HEAD = "Not kept: since these changes were started, "
TAIL = " Your project was not changed."
CHANGED, REMOVED, ADDED = "REASON_CHANGED", "REASON_REMOVED", "REASON_ADDED"


def _sentence(*rows: tuple[str, str]) -> str:
    """The sentence for (path, reason constant name) rows."""
    return snap.conflict_sentence([{"path": path, "reason": getattr(snap, reason)} for path, reason in rows])


@pytest.mark.parametrize(
    ("rows", "clause"),
    [
        ([("src/a.ts", CHANGED)], "src/a.ts was changed in your project."),
        ([("src/util.ts", REMOVED)], "src/util.ts was removed from your project."),
        ([("src/b.ts", ADDED)], "src/b.ts was added to your project."),
        ([("src/a.ts", CHANGED), ("README.md", CHANGED)], "README.md and src/a.ts were changed in your project."),
        ([("src/x.ts", REMOVED), ("src/w.ts", REMOVED)], "src/w.ts and src/x.ts were removed from your project."),
        ([("c", ADDED), ("a", ADDED), ("b", ADDED)], "a, b and c were added to your project."),
        (
            [("src/old.ts", CHANGED), ("README.md", CHANGED), ("src/a.ts", CHANGED)],
            "README.md, src/a.ts and src/old.ts were changed in your project.",
        ),
        (
            [("src/a.ts", CHANGED), ("src/util.ts", REMOVED)],
            "src/a.ts was changed in your project and src/util.ts was removed from your project.",
        ),
        (
            [("z", ADDED), ("y", REMOVED), ("x", CHANGED)],
            "x was changed in your project and y was removed from your project and z was added to your project.",
        ),
        (
            [("a", CHANGED), ("b", CHANGED), ("c", ADDED)],
            "a and b were changed in your project and c was added to your project.",
        ),
    ],
)
def test_the_sentence_names_each_file_with_what_happened_to_it(rows, clause):
    assert _sentence(*rows) == f"{HEAD}{clause}{TAIL}"


def test_the_whole_sentence_is_pinned_word_for_word():
    assert _sentence(("src/a.ts", CHANGED)) == (
        "Not kept: since these changes were started, src/a.ts was changed in your project. "
        "Your project was not changed."
    )


@pytest.mark.parametrize(
    ("rows", "count"),
    [
        (
            [("README.md", CHANGED), ("src/a.ts", CHANGED), ("src/old.ts", CHANGED), ("src/util.ts", CHANGED)],
            "1 more file",
        ),
        ([(f"f{n}.ts", CHANGED) for n in range(5)], "2 more files"),
        ([(f"f{n}.ts", CHANGED) for n in range(12)], "9 more files"),
    ],
)
def test_more_than_three_files_are_three_names_and_a_count(rows, count):
    names = ", ".join(sorted(path for path, _reason in rows)[:3])
    assert _sentence(*rows) == f"{HEAD}{names} and {count} were changed in your project.{TAIL}"


def test_the_long_form_calls_removed_and_added_files_changed():
    rows = [("d", ADDED), ("c", REMOVED), ("b", CHANGED), ("a", CHANGED)]
    assert _sentence(*rows) == f"{HEAD}a, b, c and 1 more file were changed in your project.{TAIL}"


def test_a_path_is_named_and_counted_once():
    rows = [("a", CHANGED), ("a", CHANGED), ("b", CHANGED), ("b", REMOVED), ("c", ADDED), ("c", ADDED)]
    assert _sentence(*rows) == (
        f"{HEAD}a and b were changed in your project and c was added to your project.{TAIL}"
    ), "six rows are three files: no 'and 3 more files'"


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([("src/b.ts", CHANGED), ("README.md", REMOVED), ("src/a.ts", ADDED)], id="three-mixed"),
        pytest.param(
            [("src/b.ts", CHANGED), ("README.md", REMOVED), ("src/a.ts", ADDED), ("docs/x.md", CHANGED), ("z", ADDED)],
            id="five",
        ),
    ],
)
def test_the_same_conflicts_in_any_order_read_the_same(rows):
    want = _sentence(*rows)
    for shift in range(len(rows)):
        assert _sentence(*(rows[shift:] + rows[:shift])) == want
    assert _sentence(*reversed(rows)) == want


def test_a_reason_the_builder_does_not_know_reads_as_changed():
    unknown = [{"path": "a", "reason": "something new"}]
    assert snap.conflict_sentence(unknown) == f"{HEAD}a was changed in your project.{TAIL}"


def test_no_rows_still_gives_a_true_sentence():
    assert snap.conflict_sentence([]) == f"{HEAD}some files were changed in your project.{TAIL}"
