"""B/C/U snapshot acceptance: current tree, conflicts, unrelated edits."""
from __future__ import annotations

from pathlib import Path

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
    _write(src, ".env", "TOKEN=1\n")
    dest = tmp_path / "snap"
    index = copy_snapshot(src, dest)
    assert "src/App.tsx" in index.files
    assert "notes.txt" in index.files
    assert "node_modules/pkg/index.js" not in index.files
    assert ".env" not in index.files
    assert not (dest / "node_modules").exists()


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
