"""Discard is final (cut core-fixes, problem 3).

A discarded request is closed: its snapshot offers nothing to keep or discard and shows no files and no
diff, Keep refuses it, its evidence stays on disk, and the next request never starts from the discarded
work.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from local_coder.app_service import discard_changes, keep_changes, snapshot
from local_coder.engines.opencode_config import data_root
from local_coder.general.coordinator import (
    GeneralError,
    discard_request,
    keep_request,
    list_requests,
    request_dir,
    review_payload,
    run_general_request,
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "web"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"web"}\n', encoding="utf-8")
    return root


def _change_a(candidate: Path, _text: str, _cancel) -> None:
    (candidate / "src" / "a.ts").write_text("export const a = 'CHANGED-BY-REQUEST-1'\n", encoding="utf-8")


def _add_b(candidate: Path, _text: str, _cancel) -> None:
    (candidate / "src" / "b.ts").write_text("export const b = 2\n", encoding="utf-8")


def _run(project: Path, request_id: str, engine=_change_a) -> dict:
    return run_general_request(
        project, "change it", request_id=request_id, fake_engine=engine, start_preview_after=False,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_a_waiting_result_can_be_kept_or_discarded(project):
    review = _run(project, "req-1")
    assert review["can_keep"] is True and review["can_discard"] is True
    assert review["outcome"] == "review"
    assert review["facts"]["files"] == ["src/a.ts"]


def test_the_snapshot_of_a_discarded_request_is_closed(project):
    review = _run(project, "req-1")
    discard_changes("req-1", repo=project)
    after = snapshot("req-1", repo=project)
    assert after["can_keep"] is False
    assert after["can_discard"] is False
    assert after["facts"]["files"] == []
    assert after["facts"]["delta"] == {"added": [], "modified": [], "deleted": [], "renamed": []}
    assert after["diff"] == ""
    assert after["outcome"] == "discarded" and after["facts"]["outcome"] == "discarded"
    assert "discarded" in after["summary"].lower()
    assert "CHANGED-BY-REQUEST-1" not in str(after)
    assert after["request_id"] == review["request_id"]
    assert after["preview"] == {}


def test_discard_keeps_the_evidence_and_leaves_the_project_alone(project):
    before = _tree(project)
    review = _run(project, "req-1")
    discard_changes("req-1", repo=project)
    folder = request_dir(review["project_id"], "req-1")
    assert (folder / "meta.json").is_file()
    assert "CHANGED-BY-REQUEST-1" in (folder / "candidate" / "src" / "a.ts").read_text(encoding="utf-8")
    assert _tree(project) == before


def test_history_says_discarded(project):
    _run(project, "req-1")
    discard_changes("req-1", repo=project)
    assert [(row["request_id"], row["outcome"]) for row in list_requests(project)] == [("req-1", "discarded")]


def test_keep_after_discard_is_refused_in_plain_words(project):
    before = _tree(project)
    review = _run(project, "req-1")
    discard_changes("req-1", repo=project)
    with pytest.raises(GeneralError) as raised:
        keep_changes("req-1", project)
    assert raised.value.code == "discarded"
    assert "discarded" in raised.value.detail and "nothing to keep" in raised.value.detail
    with pytest.raises(GeneralError):
        keep_request(review["project_id"], "req-1", project)
    assert _tree(project) == before, "the discarded change must not reach the project"
    assert [row["outcome"] for row in list_requests(project)] == ["discarded"]
    assert review_payload(review["project_id"], "req-1")["can_keep"] is False


def test_a_discarded_change_does_not_come_back_in_the_next_request(project):
    """The sticky working copy used to keep the discarded work, so request 2's diff showed it again."""
    _run(project, "req-1")
    discard_changes("req-1", repo=project)
    second = _run(project, "req-2", engine=_add_b)
    assert second["facts"]["files"] == ["src/b.ts"]
    assert "CHANGED-BY-REQUEST-1" not in second["diff"]
    assert (project / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 1\n"


def test_a_discarded_change_does_not_come_back_through_the_local_model_path(project):
    def first_reply(_prompt: str) -> str:
        return "I changed a.\n\n```src/a.ts\nexport const a = 'CHANGED-BY-REQUEST-1'\n```\n"

    def second_reply(_prompt: str) -> str:
        return "I added b.\n\n```src/b.ts\nexport const b = 2\n```\n"

    first = run_general_request(
        project, "change a", request_id="req-1", fake_complete=first_reply, start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert first["facts"]["files"] == ["src/a.ts"]
    discard_changes("req-1", repo=project)
    second = run_general_request(
        project, "add b", request_id="req-2", fake_complete=second_reply, start_preview_after=False,
        model_id="Qwen3.5-4B-Q4_K_M",
    )
    assert second["facts"]["files"] == ["src/b.ts"]
    assert "CHANGED-BY-REQUEST-1" not in second["diff"]


def test_resetting_the_working_copy_leaves_installed_packages_alone(project):
    review = _run(project, "req-1")
    live = data_root() / "projects" / review["project_id"] / "live" / "candidate"
    (live / "node_modules" / "pkg").mkdir(parents=True)
    (live / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    discard_changes("req-1", repo=project)
    assert (live / "node_modules" / "pkg" / "index.js").is_file()
    assert (live / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 1\n"
    assert not (live / "src" / "b.ts").exists()


def test_discarding_twice_changes_nothing_the_second_time(project):
    _run(project, "req-1")
    first = discard_changes("req-1", repo=project)
    second = discard_changes("req-1", repo=project)
    assert first["detail"] == "Discarded. The original project was not changed."
    assert second["ok"] is True and second["detail"] == "Already discarded."


def test_discard_waits_for_a_request_that_is_running_in_the_project(project):
    from local_coder.general.coordinator import _request_running

    review = _run(project, "req-1")
    with _request_running(review["project_id"]), pytest.raises(GeneralError) as raised:
        discard_changes("req-1", repo=project)
    assert raised.value.code == "busy"
    assert review_payload(review["project_id"], "req-1")["can_keep"] is True, "still waiting"
    assert discard_changes("req-1", repo=project)["discarded"] is True


def test_a_running_request_marks_its_project_while_the_engine_works(project):
    """Discarding an older request mid-run would rewrite the working copy the engine is writing to."""
    older = _run(project, "req-old")
    seen: list[str] = []

    def engine(candidate: Path, _text: str, _cancel: threading.Event | None) -> None:
        try:
            discard_request(older["project_id"], "req-old")
        except GeneralError as exc:
            seen.append(exc.code)
        _add_b(candidate, _text, _cancel)

    run_general_request(project, "add b", request_id="req-new", fake_engine=engine, start_preview_after=False)
    assert seen == ["busy"]
    # Idle again afterwards. The new request is built on the old one, so it is decided first.
    assert discard_request(older["project_id"], "req-new")["discarded"] is True
    assert discard_request(older["project_id"], "req-old")["discarded"] is True
