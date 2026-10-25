"""What the change-request prompt tells the agent about where it may build.

Seen in Nico's own requests (2026-10-23): asked from one project for a change to another (a scraper in
~/projects/scrapers/vc-jobs-board), the agent read the other project and built the scraper in the wrong one.
"""
from __future__ import annotations

from local_coder.agent.prompts import build_prompt


def test_a_change_request_stays_in_its_own_project(tmp_path):
    text = build_prompt(home=tmp_path, project=tmp_path / "tell-about-australopithecus", workdir=tmp_path / "wc")
    assert "This request can change only tell-about-australopithecus." in text
    assert "do not build it here: change nothing" in text
    assert "Reading another project to reuse its code here is fine." in text


def test_caches_go_to_the_temp_folder_not_the_project(tmp_path):
    text = build_prompt(home=tmp_path, project=tmp_path / "site", workdir=tmp_path / "wc")
    assert "use the temp folder in $TMPDIR, never a new folder inside the project" in text
