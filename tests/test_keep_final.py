"""Keep and Discard are final in both orders (cut core-fixes, fix B).

After Keep the change is in the project: the snapshot offers nothing more, a second Keep and a Discard are
refused in plain words (never "the original project was not changed"), and History says kept.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_coder.app_service import discard_changes, keep_changes, snapshot
from local_coder.general.coordinator import (
    GeneralError,
    list_requests,
    project_dir,
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
    (candidate / "src" / "a.ts").write_text("export const a = 'KEPT-CHANGE'\n", encoding="utf-8")


def _run(project: Path, request_id: str) -> dict:
    return run_general_request(
        project, "change it", request_id=request_id, fake_engine=_change_a, start_preview_after=False,
    )


def test_after_keep_the_snapshot_offers_nothing_more(project):
    _run(project, "req-1")
    keep_changes("req-1", project)
    after = snapshot("req-1", repo=project)
    assert after["can_keep"] is False
    assert after["can_discard"] is False
    assert after["outcome"] == "kept" and after["facts"]["outcome"] == "kept"
    assert after["facts"]["files"] == ["src/a.ts"], "what was kept is still described"
    assert "KEPT-CHANGE" in after["diff"]
    assert after["summary"] == "These changes are in your project."
    assert after["model_commentary"] is None


def test_a_second_keep_is_refused_and_does_not_blame_changed_files(project):
    _run(project, "req-1")
    keep_changes("req-1", project)
    (project / "src" / "a.ts").write_text("export const a = 'EDITED-AFTER-KEEP'\n", encoding="utf-8")
    with pytest.raises(GeneralError) as raised:
        keep_changes("req-1", project)
    assert raised.value.code == "kept"
    assert "already in your project" in raised.value.detail
    assert "Some files changed" not in raised.value.detail
    assert (project / "src" / "a.ts").read_text(encoding="utf-8") == "export const a = 'EDITED-AFTER-KEEP'\n"


def test_discard_after_keep_is_refused_and_the_kept_change_stays(project):
    review = _run(project, "req-1")
    keep_changes("req-1", project)
    live = project_dir(review["project_id"]) / "live" / "candidate" / "src" / "a.ts"
    with pytest.raises(GeneralError) as raised:
        discard_changes("req-1", repo=project)
    assert raised.value.code == "kept"
    assert "cannot be discarded" in raised.value.detail
    assert "original project was not changed" not in raised.value.detail.lower()
    assert "KEPT-CHANGE" in (project / "src" / "a.ts").read_text(encoding="utf-8")
    assert "KEPT-CHANGE" in live.read_text(encoding="utf-8"), "a refused Discard must not touch the working copy"
    assert [row["outcome"] for row in list_requests(project)] == ["kept"]
    assert snapshot("req-1", repo=project)["outcome"] == "kept"


def test_history_says_kept_never_discarded_after_keep(project):
    _run(project, "req-1")
    keep_changes("req-1", project)
    with pytest.raises(GeneralError):
        discard_changes("req-1", repo=project)
    assert [(row["request_id"], row["outcome"]) for row in list_requests(project)] == [("req-1", "kept")]


def test_both_flags_in_meta_json_mean_kept(project):
    """What the old bug left behind: Keep, then Discard flipped History to "discarded"."""
    review = _run(project, "req-1")
    keep_changes("req-1", project)
    meta_path = request_dir(review["project_id"], "req-1") / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["discarded"] = True
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    assert [row["outcome"] for row in list_requests(project)] == ["kept"]
    after = review_payload(review["project_id"], "req-1")
    assert after["outcome"] == "kept"
    assert after["can_keep"] is False and after["can_discard"] is False
    assert after["facts"]["files"] == ["src/a.ts"]
    assert after["diff"]


def test_a_request_that_is_still_waiting_keeps_its_choices(project):
    review = _run(project, "req-1")
    assert review["can_keep"] is True and review["can_discard"] is True
    assert review["outcome"] == "review"
    assert [row["outcome"] for row in list_requests(project)] == ["review"]
