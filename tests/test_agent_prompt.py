"""What the change-request prompt tells the agent about where it may build.

Seen in Nico's own requests (2026-10-23): asked from one project for a change to another (a scraper in
~/projects/scrapers/vc-jobs-board), the agent read the other project and built the scraper in the wrong one.
"""
from __future__ import annotations

from pathlib import Path

from local_coder.agent.prompts import ask_prompt, build_prompt


def test_a_change_request_stays_in_its_own_project(tmp_path):
    text = build_prompt(home=tmp_path, project=tmp_path / "tell-about-australopithecus", workdir=tmp_path / "wc")
    assert "This request can change only tell-about-australopithecus." in text
    assert "do not build it here: change nothing" in text
    assert "Reading another project to reuse its code here is fine." in text


def test_caches_go_to_the_temp_folder_not_the_project(tmp_path):
    text = build_prompt(home=tmp_path, project=tmp_path / "site", workdir=tmp_path / "wc")
    assert "use the temp folder in $TMPDIR, never a new folder inside the project" in text


def test_a_question_is_answered_without_a_tour_of_the_guarded_folders(tmp_path):
    text = ask_prompt(home=tmp_path, project=None)
    assert "answer it directly without searching" in text
    assert "only when the user points there" in text
    assert "Call open_project only when the user asks to open a project or to work on one" in text


def test_a_change_request_gets_to_work_without_searching(tmp_path):
    text = build_prompt(home=tmp_path, project=tmp_path / "crime-map", workdir=tmp_path / "wc")
    assert "never go searching for one. Start on the work right away." in text


def test_a_question_does_not_go_looking_for_a_matching_folder(tmp_path):
    text = ask_prompt(home=tmp_path, project=None)
    assert "Never go looking for a folder that might match what they asked for." in text


def test_the_model_talks_plainly():
    for text in (ask_prompt(home=Path("/h"), project=None),
                 build_prompt(home=Path("/h"), project=Path("/h/site"), workdir=Path("/w"))):
        assert "plain" in text and "jargon" in text
    assert "Never explain sandboxes, permissions or your own limits." in ask_prompt(home=Path("/h"), project=None)
    assert 'Say "do it" and I\'ll make the change.' in ask_prompt(home=Path("/h"), project=None)
