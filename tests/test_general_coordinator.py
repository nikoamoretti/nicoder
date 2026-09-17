"""Coordinator Keep/Discard uses snapshots, not the original tree, until Keep."""
from __future__ import annotations

from pathlib import Path

from local_coder.general.coordinator import (
    discard_request,
    keep_request,
    prepare_workspace,
    review_payload,
    run_general_request,
)


def test_fake_engine_keep_and_discard(tmp_path):
    user = tmp_path / "app"
    user.mkdir()
    (user / "src").mkdir()
    (user / "src" / "App.tsx").write_text("export default function App(){return <p>hi</p>}\n", encoding="utf-8")
    original = (user / "src" / "App.tsx").read_text(encoding="utf-8")

    def fake(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "src" / "App.tsx").write_text(
            "export default function App(){return <p>board</p>}\n", encoding="utf-8",
        )
        (candidate / "src" / "board.tsx").write_text("export const Board = () => null\n", encoding="utf-8")

    result = run_general_request(
        user, "Build a board",
        request_id="req-keep",
        fake_engine=fake,
        start_preview_after=False,
    )
    assert (user / "src" / "App.tsx").read_text(encoding="utf-8") == original
    assert result["can_keep"] is True
    assert "src/App.tsx" in result["facts"]["files"]

    kept = keep_request(result["project_id"], "req-keep", user)
    assert kept["ok"] is True
    assert "board" in (user / "src" / "App.tsx").read_text(encoding="utf-8")
    assert (user / "src" / "board.tsx").exists()


def test_discard_leaves_original(tmp_path):
    user = tmp_path / "app"
    user.mkdir()
    (user / "a.ts").write_text("1\n", encoding="utf-8")

    def fake(candidate: Path, _text: str, _cancel) -> None:
        (candidate / "a.ts").write_text("2\n", encoding="utf-8")

    result = run_general_request(
        user, "change a",
        request_id="req-discard",
        fake_engine=fake,
        start_preview_after=False,
    )
    discard_request(result["project_id"], "req-discard")
    assert (user / "a.ts").read_text(encoding="utf-8") == "1\n"


def test_starter_used_for_empty_folder(tmp_path):
    user = tmp_path / "blank"
    user.mkdir()
    meta = prepare_workspace(user, "req-empty", allow_starter=True)
    assert meta["starter_used"] is True
    assert (user / "package.json").exists()
    assert "task" not in (user / "src" / "App.tsx").read_text(encoding="utf-8").lower()
    review = review_payload(meta["project_id"], "req-empty")
    assert review["engine"] == "opencode"
