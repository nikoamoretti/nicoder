"""Acceptance gate adversaries: drift, dirt, non-solved, post-check edits."""
import shutil
import subprocess
from types import SimpleNamespace

from local_coder.cli import cmd_accept
from local_coder.inference.fake import demo_script

from .conftest import make_controller


def _git_repo(src, dest):
    shutil.copytree(src, dest)
    for args in (["init"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=dest, capture_output=True, check=True)
    return dest


def _solved_on(repo, state_dir, episode):
    controller, _ = make_controller(repo, state_dir, demo_script("fake:success"),
                                    episode_id=episode)
    state = controller.run()
    assert state.outcome == "solved"
    return controller


def _accept(episode, repo, state_dir):
    args = SimpleNamespace(episode=episode, repo=str(repo), state_dir=str(state_dir))
    return cmd_accept(args)


def test_accept_refuses_non_solved(tmp_path, state_dir):
    from .conftest import FIXTURE_REPO

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo)
    controller, _ = make_controller(repo, state_dir, demo_script("fake:blocked"),
                                    episode_id="ep-notsolved")
    assert controller.run().outcome == "blocked"
    assert _accept("ep-notsolved", repo, state_dir) == 1


def test_accept_refuses_drifted_original(tmp_path, state_dir):
    from .conftest import FIXTURE_REPO

    repo = _git_repo(FIXTURE_REPO, tmp_path / "repo")
    _solved_on(repo, state_dir, "ep-drift")
    (repo / "src/calc.py").write_text((repo / "src/calc.py").read_text() + "\n# drift\n")
    assert _accept("ep-drift", repo, state_dir) == 1


def test_accept_refuses_dirty_git_checkout(tmp_path, state_dir):
    from .conftest import FIXTURE_REPO

    repo = _git_repo(FIXTURE_REPO, tmp_path / "repo")
    _solved_on(repo, state_dir, "ep-dirty")
    (repo / "uncommitted.txt").write_text("wip\n")
    assert _accept("ep-dirty", repo, state_dir) == 1


def test_accept_refuses_candidate_changed_after_validation(tmp_path, state_dir):
    from .conftest import FIXTURE_REPO

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo)
    controller = _solved_on(repo, state_dir, "ep-stale")
    cand = controller.store.candidate_dir("ep-stale") / "src/calc.py"
    cand.write_text(cand.read_text() + "\n# post-hoc\n")
    assert _accept("ep-stale", repo, state_dir) == 1


def test_accept_applies_clean_git_repo(tmp_path, state_dir):
    from .conftest import FIXTURE_REPO

    repo = _git_repo(FIXTURE_REPO, tmp_path / "repo")
    _solved_on(repo, state_dir, "ep-clean")
    assert _accept("ep-clean", repo, state_dir) == 0
    assert "return a + b" in (repo / "src/calc.py").read_text()
