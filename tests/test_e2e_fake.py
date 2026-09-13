"""End-to-end offline episodes: solved, failing, blocked, inspectable."""
import json
import subprocess
import sys
from pathlib import Path

from local_coder.inference.fake import demo_script
from local_coder.paths import original_manifest, tree_hash
from local_coder.persistence import EpisodeStore

from .conftest import FIXTURE_DIR, make_controller


def test_success_episode_leaves_original_untouched(repo_copy, state_dir):
    before = tree_hash(original_manifest(repo_copy))
    before_bytes = (repo_copy / "src/calc.py").read_bytes()
    controller, adapter = make_controller(repo_copy, state_dir, demo_script("fake:success"),
                                          profile="fake:success", episode_id="ep-success")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.generations_used == 4
    assert adapter.calls == 4
    assert state.provenance["adapter"] == "fake"
    assert "NON-SANDBOXED" in state.provenance["isolation"]
    # Original source is byte-identical; the fix lives only in the candidate.
    assert tree_hash(original_manifest(repo_copy)) == before
    assert (repo_copy / "src/calc.py").read_bytes() == before_bytes
    candidate_fixed = (controller.workspace.root / "src/calc.py").read_text()
    assert "return a + b" in candidate_fixed
    # Evidence bundle is complete and inspectable.
    epdir = controller.store.episode_dir("ep-success")
    for name in ("state.json", "briefing.txt", "events.jsonl", "handoff.md"):
        assert (epdir / name).exists(), name
    assert (epdir / "artifacts/final.diff").exists()
    final_diff = (epdir / "artifacts/final.diff").read_text()
    assert "return a + b" in final_diff
    logs = sorted((epdir / "artifacts").glob("check-*.log"))
    assert len(logs) >= 3  # baseline + focused + final
    assert (epdir / "artifacts/search-0001.json").exists()
    assert (epdir / "artifacts/patch-0002.diff").exists()
    handoff = (epdir / "handoff.md").read_text()
    assert "Outcome: solved" in handoff
    # Stored results reload after a restart.
    reloaded = EpisodeStore(state_dir).load_state("ep-success")
    assert reloaded.outcome == "solved"
    assert reloaded.final.status == "pass"


def test_blocked_episode_handoff_is_useful(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:blocked"),
                                    episode_id="ep-blocked")
    state = controller.run()
    assert state.outcome == "blocked"
    assert "insufficient_evidence" in (state.stop_reason or "")
    handoff = (controller.store.episode_dir("ep-blocked") / "handoff.md").read_text()
    assert "subtracts instead of adding" in handoff  # goal
    assert "## Relevant files" in handoff
    assert "## Attempted changes" in handoff
    assert "(no patches applied)" in handoff


def _cli_env():
    import os

    env = dict(os.environ)
    base = Path(__file__).parent.parent / "src"
    env["PYTHONPATH"] = str(base) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # The fixture registry invokes `python3`; pin it to the interpreter under test.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def test_cli_run_inspect_diff_accept(repo_copy, state_dir, tmp_path):
    py = sys.executable
    common = ["--state-dir", str(state_dir)]
    run = subprocess.run(
        [py, "-m", "local_coder.cli", *common, "run", "--repo", str(repo_copy),
         "--ticket-file", str(FIXTURE_DIR / "ticket.md"), "--profile", "fake:success",
         "--execution", "trusted-local", "--episode-id", "ep-cli"],
        capture_output=True, text=True, env=_cli_env(), timeout=300)
    assert run.returncode == 0, run.stderr
    assert "FAKE" in run.stdout and "outcome: solved" in run.stdout
    inspect = subprocess.run(
        [py, "-m", "local_coder.cli", *common, "inspect", "ep-cli"],
        capture_output=True, text=True, env=_cli_env(), timeout=60)
    assert inspect.returncode == 0, inspect.stderr
    json_part = "\n".join(line for line in inspect.stdout.splitlines()
                          if not line.startswith("  #"))
    payload = json.loads(json_part)
    assert payload["outcome"] == "solved" and payload["events"] > 10
    diff = subprocess.run(
        [py, "-m", "local_coder.cli", *common, "diff", "ep-cli"],
        capture_output=True, text=True, env=_cli_env(), timeout=60)
    assert diff.returncode == 0 and "return a + b" in diff.stdout
    accept = subprocess.run(
        [py, "-m", "local_coder.cli", *common, "accept", "ep-cli", "--repo", str(repo_copy)],
        capture_output=True, text=True, env=_cli_env(), timeout=60)
    assert accept.returncode == 0, accept.stderr
    assert "return a + b" in (repo_copy / "src/calc.py").read_text()


def test_discard_removes_candidate_keeps_audit(repo_copy, state_dir):
    from types import SimpleNamespace

    from local_coder.cli import cmd_discard, cmd_inspect

    controller, _ = make_controller(repo_copy, state_dir, demo_script("fake:blocked"),
                                    episode_id="ep-discard")
    controller.run()
    epdir = controller.store.episode_dir("ep-discard")
    assert (epdir / "candidate").exists()
    args = SimpleNamespace(episode="ep-discard", with_evidence=False, state_dir=str(state_dir))
    assert cmd_discard(args) == 0
    assert not (epdir / "candidate").exists()
    assert (epdir / "handoff.md").exists() and (epdir / "events.jsonl").exists()
    args2 = SimpleNamespace(episode="ep-discard", state_dir=str(state_dir))
    assert cmd_inspect(args2) == 0
