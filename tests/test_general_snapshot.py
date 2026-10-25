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


def test_sync_source_tree_skip_same_leaves_unchanged_files_alone(tmp_path):
    """A dev server watching dest sees a rewritten file as changed, same bytes or not: skip_same does not write it."""
    from local_coder.general.snapshot import sync_source_tree

    source, dest = tmp_path / "user", tmp_path / "copy"
    _write(source, "src/App.tsx", "one\n")
    _write(source, "vite.config.ts", "config\n")
    _write(source, "gone.ts", "x\n")
    sync_source_tree(source, dest)
    old = 1_000_000_000
    for rel in ("src/App.tsx", "vite.config.ts", "gone.ts"):
        os.utime(dest / rel, (old, old))
    (source / "gone.ts").unlink()
    _write(source, "src/App.tsx", "two\n")
    _write(source, "src/New.tsx", "new\n")
    sync_source_tree(source, dest, skip_same=True)
    assert (dest / "vite.config.ts").stat().st_mtime == old, "same bytes: not written"
    assert (dest / "src" / "App.tsx").read_text(encoding="utf-8") == "two\n", "changed: written"
    assert (dest / "src" / "New.tsx").read_text(encoding="utf-8") == "new\n"
    assert not (dest / "gone.ts").exists(), "removed from the source: removed"
    # the default still rewrites everything, as the requests rely on
    os.utime(dest / "vite.config.ts", (old, old))
    sync_source_tree(source, dest)
    assert (dest / "vite.config.ts").stat().st_mtime != old


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


def test_a_path_that_leaves_the_project_is_refused_before_anything_is_written(tmp_path):
    """His `src` became a link to a folder elsewhere: writing src/new.ts would land outside the project. The request
    also deletes an executable, and the refusal must come before that delete."""
    b, c, u = _trees(
        tmp_path,
        {**README, "run.sh": "#!/bin/sh\necho hi\n"},
        {**README, "src/new.ts": MOVED},
        {**README, "run.sh": "#!/bin/sh\necho hi\n"},
    )
    (b / "run.sh").chmod(0o755)
    (u / "run.sh").chmod(0o755)
    os.utime(u / "run.sh", ns=(LONG_AGO, LONG_AGO))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (u / "src").symlink_to(elsewhere)
    before = _state(u)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert _state(u) == before and (u / "run.sh").stat().st_mtime_ns == LONG_AGO
    assert list(elsewhere.iterdir()) == [], "nothing landed outside the project"


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
    b, c, u = _trees(tmp_path, {"a.ts": "one\n"}, {"a.ts": "two\n"}, {"a.ts": "his\n"})
    result = apply_delta(u, b, c)
    assert result["detail"].startswith("Some files changed in the project")
    assert not result["detail"].startswith("Not kept")
    assert result["conflicts"] == [
        {"path": "a.ts", "reason": "this file changed in the project after the request started"},
    ]


# --- Late failures: what Keep can know is refused before the first write -----------------------------------------
#
# Every scenario below deletes an executable (run.sh, mode 0755, mtime far in the past) besides what it adds, and the
# project holds something in the way. A refusal found halfway would have deleted run.sh first and rolled it back from
# its bytes: a new file with mode 0644 and a new mtime. So each refusal compares the whole folder, modes and times
# included. Names LTn / LGn are the cut's probe scenarios (LT the gaps, LG the guards).

SH = "#!/bin/sh\necho hi\n"


def _stamps(root: Path) -> dict[str, tuple]:
    """`_state` plus the mtime of every file in nanoseconds: 'nothing was touched' includes the clock, which a
    rollback from bytes moves (it brings a deleted executable back as a new file)."""
    found = _state(root)
    for rel, row in found.items():
        if row[0] == "file":
            found[rel] = (*row, (root / rel).stat().st_mtime_ns)
    return found


def _late_trees(
    tmp_path: Path, result: dict[str, str], *, start: dict[str, str] | None = None, now: dict[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    """B, C and U for a request that DELETES the executable run.sh and writes `result` besides.

    B is the README, run.sh and `start`; C is the README and `result`; U is B and `now`."""
    before = {**README, "run.sh": SH, **(start or {})}
    b, c, u = _trees(tmp_path, before, {**README, **result}, {**before, **(now or {})})
    (b / "run.sh").chmod(0o755)
    (u / "run.sh").chmod(0o755)
    os.utime(u / "run.sh", ns=(LONG_AGO, LONG_AGO))
    return b, c, u


def _refused_before_writing(b: Path, c: Path, u: Path, *, locked: Path | None = None) -> dict:
    """Keep is refused as `apply_failed` and nothing was written: the folder is as it was, modes and times included,
    and the executable the request deletes is still 0755 and untouched. `locked` is a folder made unsearchable while
    Keep runs (its permissions come back before the next line, or the temp folder could not be removed)."""
    before = _stamps(u)
    if locked is not None:
        locked.chmod(0o000)
    try:
        result = apply_delta(u, b, c)
    finally:
        if locked is not None:
            locked.chmod(0o755)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert _stamps(u) == before, "the folder is byte-identical, modes and times included"
    assert stat.S_IMODE((u / "run.sh").stat().st_mode) == 0o755 and (u / "run.sh").stat().st_mtime_ns == LONG_AGO
    return result


def test_lt1_a_file_where_a_folder_must_be_made_is_refused_before_anything_is_written(tmp_path):
    b, c, u = _late_trees(tmp_path, {"lib/x.ts": MOVED}, now={"lib": HIS})
    _refused_before_writing(b, c, u)
    assert (u / "lib").is_file() and (u / "lib").read_text(encoding="utf-8") == HIS


@pytest.mark.parametrize("with_a_file_of_his", [True, False], ids=["lt2_with_a_file_in_it", "lt2b_empty"])
def test_lt2_a_folder_where_an_added_file_goes_is_refused_before_anything_is_written(tmp_path, with_a_file_of_his):
    """An empty folder too: the snapshot lists files only, so neither one shows up as a clash."""
    b, c, u = _late_trees(tmp_path, {"b.ts": MOVED})
    (u / "b.ts").mkdir()
    if with_a_file_of_his:
        (u / "b.ts" / "his.txt").write_text(HIS, encoding="utf-8")
    _refused_before_writing(b, c, u)
    assert (u / "b.ts").is_dir()
    assert not with_a_file_of_his or (u / "b.ts" / "his.txt").read_text(encoding="utf-8") == HIS


def test_lt3_a_hand_made_move_with_a_folder_at_the_source_is_refused_before_anything_is_written(tmp_path):
    b, c, u = _late_trees(tmp_path, {"src/new.ts": MOVED}, start={"src/old.ts": MOVED})
    (u / "src" / "old.ts").rename(u / "src" / "new.ts")  # he made the same move...
    (u / "src" / "old.ts").mkdir()  # ...and put a folder where the file was
    _refused_before_writing(b, c, u)
    assert (u / "src" / "old.ts").is_dir() and (u / "src" / "new.ts").read_text(encoding="utf-8") == MOVED


def test_lt4_a_rename_into_a_new_folder_with_his_file_in_the_way_is_refused_before_anything_is_written(tmp_path):
    b, c, u = _late_trees(tmp_path, {"lib/new.ts": MOVED}, start={"src/old.ts": MOVED}, now={"lib": HIS})
    _refused_before_writing(b, c, u)
    assert (u / "src" / "old.ts").read_text(encoding="utf-8") == MOVED, "the move never started"
    assert (u / "lib").read_text(encoding="utf-8") == HIS


def test_lt5_a_dangling_link_as_the_parent_folder_is_refused_before_anything_is_written(tmp_path):
    b, c, u = _late_trees(tmp_path, {"d/a.ts": MOVED})
    (u / "d").symlink_to("nowhere-at-all")
    _refused_before_writing(b, c, u)
    assert os.readlink(u / "d") == "nowhere-at-all"


def test_lt6_a_link_that_loops_at_an_added_path_is_a_refusal_not_an_unhandled_error(tmp_path):
    """`Path.resolve()` raises RuntimeError on a looping link (Python 3.12), which `except OSError` did not catch."""
    b, c, u = _late_trees(tmp_path, {"a.ts": MOVED})
    (u / "a.ts").symlink_to("a.ts")
    _refused_before_writing(b, c, u)
    assert os.readlink(u / "a.ts") == "a.ts"


def test_lt7_a_link_that_loops_as_the_parent_folder_is_a_refusal_not_an_unhandled_error(tmp_path):
    b, c, u = _late_trees(tmp_path, {"d/a.ts": MOVED})
    (u / "d").symlink_to("d")
    _refused_before_writing(b, c, u)
    assert os.readlink(u / "d") == "d"


def test_lt9_a_file_two_folders_up_is_refused_before_anything_is_written(tmp_path):
    b, c, u = _late_trees(tmp_path, {"a/b/c.ts": MOVED}, now={"a": HIS})
    _refused_before_writing(b, c, u)
    assert (u / "a").is_file() and (u / "a").read_text(encoding="utf-8") == HIS


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any folder")
def test_lt10_a_folder_he_cannot_search_is_a_refusal_not_an_unhandled_error(tmp_path):
    """The check itself meets the permission error (`lstat` of a path inside the folder): every line of it is inside
    the one `try`, not only the first two loops."""
    b, c, u = _late_trees(tmp_path, {"sub/x.ts": MOVED}, now={"sub/keep.txt": "k\n"})
    _refused_before_writing(b, c, u, locked=u / "sub")
    assert not (u / "sub" / "x.ts").exists() and (u / "sub" / "keep.txt").read_text(encoding="utf-8") == "k\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any folder")
def test_lt10b_a_rename_inside_a_folder_he_cannot_search_is_a_refusal_not_an_unhandled_error(tmp_path):
    """The clash check meets it first: it looks at a rename's two ends on the disk (`is_symlink`), which raised
    PermissionError inside the folder before the pre-flight even started. One `OSError` guard around the clash check
    gives the same plain refusal as the other late failures, and nothing is written."""
    b, c, u = _late_trees(tmp_path, {"sub/new.ts": MOVED}, start={"sub/old.ts": MOVED})
    assert compute_delta(index_tree(b), index_tree(c)).renamed == [("sub/old.ts", "sub/new.ts")], "a real rename"
    _refused_before_writing(b, c, u, locked=u / "sub")
    assert (u / "sub" / "old.ts").read_text(encoding="utf-8") == MOVED and not (u / "sub" / "new.ts").exists()


def test_a_looping_link_met_by_the_check_is_a_refusal_whatever_the_python_version(tmp_path, monkeypatch):
    """LT6 and LT7 need a Python whose `resolve()` raises on a loop; this one makes it raise."""
    b, c, u = _late_trees(tmp_path, {"a.ts": MOVED})

    def loops(root: Path, rel: str) -> None:
        raise RuntimeError(f"Symlink loop from '{root / rel}'")

    monkeypatch.setattr(snap, "_assert_inside", loops)
    result = _refused_before_writing(b, c, u)
    assert "Symlink loop" in str(result["detail"])


def test_a_loop_that_appears_after_the_check_is_rolled_back_not_raised(tmp_path, monkeypatch):
    """The write loop catches RuntimeError as well as OSError: a loop that shows up between the check and a write
    (his link, made meanwhile) is a refusal with the project restored from its bytes, not an unhandled error."""
    b, c, u = _late_trees(tmp_path, {"a.ts": MOVED})
    real_check, real_inside = snap._blocked_before_writing, snap._assert_inside
    over = {"check": False}

    def checked(user: Path, candidate: Path, delta: snap.Delta) -> str | None:
        reason = real_check(user, candidate, delta)
        over["check"] = True  # from here on the writes begin
        return reason

    def inside(root: Path, rel: str) -> None:
        if over["check"] and rel == "a.ts":
            raise RuntimeError(f"Symlink loop from '{root / rel}'")
        real_inside(root, rel)

    monkeypatch.setattr(snap, "_blocked_before_writing", checked)
    monkeypatch.setattr(snap, "_assert_inside", inside)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert "Symlink loop" in str(result["detail"])
    assert (u / "run.sh").read_text(encoding="utf-8") == SH, "the delete was rolled back"
    assert not (u / "a.ts").exists() and (u / "README.md").read_text(encoding="utf-8") == README["README.md"]


def test_a_disk_error_found_only_while_writing_is_still_rolled_back_from_its_bytes(tmp_path, monkeypatch):
    """RL1: what Keep cannot know is unchanged: the second write fails, everything is put back from the bytes. (Named
    limit, a later cut, `rollback-keeps-modes`: the deleted executable comes back as a new file.)"""
    b, c, u = _late_trees(tmp_path, {"src/x.ts": MOVED, "src/y.ts": OTHER})
    real, calls = snap._atomic_copy, {"n": 0}

    def fails_on_the_second(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device", str(dst))
        real(src, dst)

    monkeypatch.setattr(snap, "_atomic_copy", fails_on_the_second)
    result = apply_delta(u, b, c)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert (u / "run.sh").read_text(encoding="utf-8") == SH
    assert not (u / "src" / "x.ts").exists() and not (u / "src" / "y.ts").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into any folder")
def test_a_read_only_folder_is_a_disk_error_found_only_while_writing(tmp_path):
    """RL2: Keep does not guess permissions; the write fails and is rolled back from the bytes."""
    b, c, u = _late_trees(tmp_path, {"sub/x.ts": MOVED}, now={"sub/keep.txt": "k\n"})
    (u / "sub").chmod(0o555)
    try:
        result = apply_delta(u, b, c)
    finally:
        (u / "sub").chmod(0o755)
    assert result["ok"] is False and result["code"] == "apply_failed" and result["applied"] == []
    assert (u / "run.sh").read_text(encoding="utf-8") == SH and not (u / "sub" / "x.ts").exists()
    assert (u / "sub" / "keep.txt").read_text(encoding="utf-8") == "k\n"


def test_lg1_folders_that_do_not_exist_yet_are_made(tmp_path):
    b, c, u = _late_trees(tmp_path, {"deep/er/x.ts": MOVED})
    result = apply_delta(u, b, c)
    assert result["ok"] is True and result["applied"] == ["deep/er/x.ts", "run.sh"]
    assert (u / "deep" / "er" / "x.ts").read_text(encoding="utf-8") == MOVED and not (u / "run.sh").exists()


def test_lg2_an_existing_empty_folder_is_a_fine_parent(tmp_path):
    b, c, u = _late_trees(tmp_path, {"lib/x.ts": MOVED})
    (u / "lib").mkdir()
    result = apply_delta(u, b, c)
    assert result["ok"] is True and (u / "lib" / "x.ts").read_text(encoding="utf-8") == MOVED


def test_lg3_a_move_he_made_by_hand_still_goes_through(tmp_path):
    b, c, u = _late_trees(tmp_path, {"src/new.ts": MOVED}, start={"src/old.ts": MOVED})
    (u / "src" / "old.ts").rename(u / "src" / "new.ts")
    result = apply_delta(u, b, c)
    assert result["ok"] is True
    assert (u / "src" / "new.ts").read_text(encoding="utf-8") == MOVED
    assert not (u / "src" / "old.ts").exists() and not (u / "run.sh").exists()


def test_lg6_a_link_to_a_folder_inside_the_project_is_a_parent_as_before(tmp_path):
    """Only a parent that is not a folder is refused. A link to a folder is one, and the write goes through it."""
    b, c, u = _late_trees(tmp_path, {"d/a.ts": MOVED})
    (u / "real").mkdir()
    (u / "d").symlink_to("real")
    result = apply_delta(u, b, c)
    assert result["ok"] is True and (u / "real" / "a.ts").read_text(encoding="utf-8") == MOVED


# LG7: a parent that this Keep itself removes is no obstacle. The check reads the disk before the writes, and the
# writes delete a file (or move it away) before they make the folder in its place.
LG7_KEEPS = [
    pytest.param(
        {"config": "A=1\n"}, {"config/dev.json": "{}\n", "config/prod.json": '{"p": 1}\n'},
        ["config", "config/dev.json", "config/prod.json"], id="lg7a_a_file_replaced_by_a_folder_of_files",
    ),
    pytest.param(
        {"scripts/deploy": SH}, {"scripts/deploy/run.sh": SH.replace("hi", "hello")},
        ["scripts/deploy", "scripts/deploy/run.sh"], id="lg7b_a_script_replaced_by_a_folder_with_one_inside",
    ),
    pytest.param(
        {"lib": "text\n"}, {"lib.old": "text\n", "lib/x.ts": "new\n"},
        ["lib", "lib.old", "lib/x.ts"], id="lg7c_a_file_moved_away_and_a_folder_made_where_it_was",
    ),
    pytest.param(
        {"lib": "LIB\n", "x.ts": "X\n"}, {"lib.old": "LIB\n", "lib/x.ts": "X\n"},
        ["lib", "lib.old", "lib/x.ts", "x.ts"], id="lg7d_a_file_moved_into_the_folder_that_replaces_another",
    ),
]


@pytest.mark.parametrize(("start", "result", "applied"), LG7_KEEPS)
def test_lg7_a_parent_that_this_keep_removes_or_moves_away_is_no_obstacle(tmp_path, start, result, applied):
    b, c, u = _trees(tmp_path, {**README, **start}, {**README, **result}, {**README, **start})
    kept = apply_delta(u, b, c)
    assert kept["ok"] is True and kept["applied"] == applied
    assert _state(u) == _state(c), "the project is the result, file for file"
    assert all((u / name).is_file() for name in result)


def _disk_ignores_case(root: Path) -> bool:
    """True when this disk does not tell `X` from `x` (macOS by default)."""
    probe = root / "case-probe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (root / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_lg7e_a_file_replaced_by_a_folder_that_differs_only_in_case_goes_through(tmp_path):
    """macOS does not tell `Docs` from `docs`: Keep deletes the file and makes the folder, as it always did. Comparing
    the removed file's exact name with the parent refused this for good ("not a folder: docs")."""
    if not _disk_ignores_case(tmp_path):
        pytest.skip("this disk tells Docs from docs, so there is no such replacement to make")
    start = {**README, "Docs": "A file named Docs\n"}
    b, c, u = _trees(tmp_path, start, {**README, "docs/readme.md": "# docs\n"}, start)
    kept = apply_delta(u, b, c)
    assert kept["ok"] is True, kept
    assert (u / "docs").is_dir() and (u / "docs" / "readme.md").read_text(encoding="utf-8") == "# docs\n"


@pytest.mark.parametrize(
    ("start", "result"),
    [
        pytest.param(
            {"lib": "same\n"}, {"lib/index.txt": "same\n"}, id="a_file_moved_into_a_folder_made_at_its_own_name",
        ),
        pytest.param(
            {"a.ts": "A\n", "lib": "LIB\n"}, {"lib/a.ts": "A\n", "lib.old": "LIB\n"},
            id="a_move_into_the_folder_comes_before_the_move_that_clears_it",
        ),
    ],
)
def test_the_writes_make_a_folder_before_a_later_move_clears_its_name_so_the_check_refuses(tmp_path, start, result):
    """The check follows the order of the writes: deletes, each rename (its folder first, then the move), then the
    adds. A file still there when a folder is made on its name stops the write (Base fails these halfway too, with a
    rollback from bytes), so it is refused before the first one."""
    b, c, u = _late_trees(tmp_path, result, start=start)
    _refused_before_writing(b, c, u)
    assert all((u / name).is_file() for name in start)


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
