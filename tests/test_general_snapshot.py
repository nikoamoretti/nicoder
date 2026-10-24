"""B/C/U snapshot acceptance: current tree, conflicts, unrelated edits."""
from __future__ import annotations

import os
import stat
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


# --- Cut keep-honesty: a rename is checked at both ends, before anything is written -------------------------------

MOVED = "export const moved = 'X'\n"
OTHER = "export const other = 'Y'\n"
HIS = "export const nico = 'MINE'\n"
README = {"README.md": "# p\n"}
LONG_AGO = 1_000_000_000_000_000_000  # ns: any rewrite of a file moves its mtime off this


def _trees(
    tmp_path: Path, before: dict[str, str], after: dict[str, str], now: dict[str, str],
) -> tuple[Path, Path, Path]:
    """B (the request's start), C (its finished candidate) and U (the project now), each from a {path: text} map."""
    roots = []
    for name, files in (("B", before), ("C", after), ("U", now)):
        root = tmp_path / name
        root.mkdir()
        for rel, text in files.items():
            _write(root, rel, text)
        roots.append(root)
    return roots[0], roots[1], roots[2]


def _state(root: Path) -> dict[str, tuple]:
    """Everything under root: each file's bytes and mode, each link's target, each folder. 'Nothing was written'
    means this is equal before and after."""
    found: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            found[rel] = ("folder",)
        else:
            found[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return found


def _conflicts(b: Path, c: Path, u: Path) -> list[tuple[str, str]]:
    return [(row.path, row.reason) for row in find_conflicts(index_tree(b), index_tree(c), index_tree(u))]


def _rename_trees(tmp_path: Path, now: dict[str, str]) -> tuple[Path, Path, Path]:
    """The request moves src/old.ts to src/new.ts; `now` is what the project holds besides the README."""
    return _trees(tmp_path, {**README, "src/old.ts": MOVED}, {**README, "src/new.ts": MOVED}, {**README, **now})


def test_a_file_he_made_at_a_renames_destination_is_a_conflict_of_the_added_kind(tmp_path):
    b, c, u = _rename_trees(tmp_path, {"src/old.ts": MOVED, "src/new.ts": HIS})
    assert _conflicts(b, c, u) == [("src/new.ts", snap.REASON_ADDED)]
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "conflict" and result["applied"] == []
    assert _state(u) == before, "his file is still there and the source was not moved"


@pytest.mark.parametrize(
    "now",
    [
        pytest.param({}, id="deleted"),
        pytest.param({"src/elsewhere.ts": MOVED}, id="moved-elsewhere"),
    ],
)
def test_a_rename_whose_source_he_removed_is_a_conflict_of_the_removed_kind(tmp_path, now):
    b, c, u = _rename_trees(tmp_path, now)
    assert _conflicts(b, c, u) == [("src/old.ts", snap.REASON_REMOVED)]
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "conflict" and result["applied"] == []
    assert _state(u) == before, "the destination is not made out of nothing"


def test_a_rename_whose_source_he_edited_is_a_conflict(tmp_path):
    b, c, u = _rename_trees(tmp_path, {"src/old.ts": "export const moved = 'EDITED BY NICO'\n"})
    assert [path for path, _reason in _conflicts(b, c, u)] == ["src/old.ts"]
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "conflict"
    assert _state(u) == before


def test_he_deleted_the_source_and_made_his_own_destination_is_two_conflicts(tmp_path):
    b, c, u = _rename_trees(tmp_path, {"src/new.ts": HIS})
    assert _conflicts(b, c, u) == [("src/new.ts", snap.REASON_ADDED), ("src/old.ts", snap.REASON_REMOVED)]


def test_a_rename_onto_the_text_the_request_wrote_is_no_conflict(tmp_path):
    """The file at the destination is exactly what the request put there: moving over it loses nothing."""
    b, c, u = _rename_trees(tmp_path, {"src/old.ts": MOVED, "src/new.ts": MOVED})
    assert _conflicts(b, c, u) == []
    assert apply_delta(u, b, c)["ok"] is True
    assert (u / "src" / "new.ts").read_text(encoding="utf-8") == MOVED and not (u / "src" / "old.ts").exists()


def test_a_move_he_made_by_hand_is_no_conflict_and_still_counts_as_applied(tmp_path):
    """Source gone, destination exactly as the request has it: nothing to do for the pair, the rest still lands."""
    b, c, u = _trees(
        tmp_path,
        {**README, "src/old.ts": MOVED},
        {"README.md": "# p\nrenamed\n", "src/new.ts": MOVED},
        {**README, "src/new.ts": MOVED},
    )
    assert _conflicts(b, c, u) == []
    result = apply_delta(u, b, c)
    assert result["ok"] is True and result["applied"] == ["README.md", "src/new.ts", "src/old.ts"]
    assert (u / "README.md").read_text(encoding="utf-8") == "# p\nrenamed\n"
    assert (u / "src" / "new.ts").read_text(encoding="utf-8") == MOVED and not (u / "src" / "old.ts").exists()


def test_a_case_only_rename_is_no_clash_with_itself(tmp_path):
    """The project's disk ignores case: a rename that only changes the case must not look like a taken name."""
    b, c, u = _trees(
        tmp_path,
        {**README, "src/Widget.ts": MOVED},
        {**README, "src/widget.ts": MOVED},
        {**README, "src/Widget.ts": MOVED},
    )
    assert _conflicts(b, c, u) == []
    assert apply_delta(u, b, c)["ok"] is True
    assert sorted(path.name for path in (u / "src").iterdir()) == ["widget.ts"]


@pytest.mark.parametrize(
    ("end", "reason"),
    [("src/new.ts", "REASON_ADDED"), ("src/old.ts", "REASON_CHANGED")],
    ids=["destination", "source"],
)
def test_a_link_at_either_end_of_a_rename_is_a_conflict_and_writes_nothing(tmp_path, end, reason):
    """The indexes skip links, so this is decided on the disk: a link at the destination reads as added, one at the
    source as changed (never as removed), and the generic 'could not write' refusal is not what he gets."""
    b, c, u = _rename_trees(tmp_path, {"src/old.ts": MOVED})
    if end == "src/old.ts":
        (u / "src" / "old.ts").unlink()
    (u / end).symlink_to("../README.md")
    assert _conflicts(b, c, u) == [(end, getattr(snap, reason))]
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "conflict" and result["applied"] == []
    assert _state(u) == before and os.readlink(u / end) == "../README.md"


def test_two_renames_one_clashing_write_nothing_and_leave_every_mode_alone(tmp_path):
    """Both ends of every rename are checked before the first move. A check inside the loop would have moved a.ts
    first and rolled it back as a new file: right bytes, mode 0644, new mtime."""
    b, c, u = _trees(
        tmp_path,
        {"a.ts": MOVED, "b.ts": OTHER},
        {"a2.ts": MOVED, "b2.ts": OTHER},
        {"a.ts": MOVED, "b.ts": OTHER, "b2.ts": HIS},
    )
    for root, name in ((b, "a.ts"), (c, "a2.ts"), (u, "a.ts")):
        (root / name).chmod(0o755)
    os.utime(u / "a.ts", ns=(LONG_AGO, LONG_AGO))
    assert _conflicts(b, c, u) == [("b2.ts", snap.REASON_ADDED)]
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "conflict" and result["applied"] == []
    assert _state(u) == before and stat.S_IMODE((u / "a.ts").stat().st_mode) == 0o755
    assert (u / "a.ts").stat().st_mtime_ns == LONG_AGO, "the first rename's source was never touched"


def test_a_link_where_the_request_adds_a_file_is_refused_before_anything_is_written(tmp_path):
    """The request deletes an executable and adds src/new.ts; he made a link there. The delete must not happen
    first: a rollback would bring the script back as a new file (mode 0644, new mtime)."""
    b, c, u = _trees(
        tmp_path,
        {**README, "run.sh": "#!/bin/sh\necho hi\n"},
        {**README, "src/new.ts": MOVED},
        {**README, "run.sh": "#!/bin/sh\necho hi\n"},
    )
    (b / "run.sh").chmod(0o755)
    (u / "run.sh").chmod(0o755)
    os.utime(u / "run.sh", ns=(LONG_AGO, LONG_AGO))
    (u / "src").mkdir()
    (u / "src" / "new.ts").symlink_to("../README.md")
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert _state(u) == before and (u / "run.sh").stat().st_mtime_ns == LONG_AGO


def test_a_folder_at_a_renames_destination_is_refused_before_anything_is_written(tmp_path):
    """Not a clash he can see in a file list: the refusal stays the generic one, and nothing is touched."""
    b, c, u = _rename_trees(tmp_path, {"src/old.ts": MOVED})
    (u / "src" / "new.ts").mkdir()
    (u / "src" / "new.ts" / "keep.txt").write_text("his\n", encoding="utf-8")
    os.utime(u / "src" / "old.ts", ns=(LONG_AGO, LONG_AGO))
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert _state(u) == before and (u / "src" / "old.ts").stat().st_mtime_ns == LONG_AGO


def test_conflicts_come_sorted_by_path_and_once_per_path(tmp_path):
    names = [f"src/{letter}.ts" for letter in "fbdace"]
    b, c, u = _trees(
        tmp_path,
        {name: "one\n" for name in names},
        {name: "two\n" for name in names},
        {name: "his\n" for name in names},
    )
    found = _conflicts(b, c, u)
    assert [path for path, _reason in found] == sorted(names)
    assert {reason for _path, reason in found} == {snap.REASON_CHANGED}


def test_apply_delta_keeps_its_own_detail_and_list_for_the_mcp_apply(tmp_path):
    """`nemotron_apply_draft` prints this detail and this list as they are; Nico's sentence is built elsewhere."""
    b, c, u = _rename_trees(tmp_path, {"src/old.ts": MOVED, "src/new.ts": HIS})
    result = apply_delta(u, b, c)
    assert result["detail"].startswith("Some files changed in the project")
    assert not result["detail"].startswith("Not kept")
    assert result["conflicts"] == [{"path": "src/new.ts", "reason": snap.REASON_ADDED}]


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
