"""Prompt 2 CHECK adversaries: timeouts, floods, tampering, symlinks, drift."""
import shutil
import time

import pytest

from local_coder.execution.trusted_local import TrustedLocalBackend
from local_coder.paths import candidate_manifest
from local_coder.skills import check as check_skill
from local_coder.workspace import CandidateWorkspace, PatchValidationError

from .conftest import fixture_paths, make_controller


def _run(controller, scope, timeout=60, log="check-adv.log"):
    return check_skill.execute_scope(
        controller.state, controller.workspace, TrustedLocalBackend(), scope,
        controller.config.commands[scope], timeout, 65536, log,
        controller.store.write_artifact_text, controller.store.write_artifact_bytes)


def test_command_timeout_kills_process_and_is_incomplete(tmp_path, state_dir):
    repo = tmp_path / "slow"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(12)\n")
    (repo / "conftest.py").write_text("")
    (repo / "local-coder.toml").write_text("")
    controller, _ = make_controller(repo, state_dir, [])
    controller.start()
    started = time.monotonic()
    evidence = _run(controller, "focused", timeout=3)
    elapsed = time.monotonic() - started
    assert evidence.status == "incomplete"
    assert "timeout" in (evidence.incomplete_reason or "")
    assert elapsed < 30, "timed-out command must not linger"


def test_log_flood_truncates_but_keeps_valid_evidence(tmp_path, state_dir):
    repo = tmp_path / "flood"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_flood.py").write_text(
        "def test_loud():\n    print('x' * 100000)\n    assert True\n")
    (repo / "conftest.py").write_text("")
    (repo / "local-coder.toml").write_text("")
    controller, _ = make_controller(repo, state_dir, [])
    controller.start()
    # Trusted registry tweak (not model-visible): show passing stdout.
    controller.config.commands["focused"] = [*controller.config.commands["focused"], "-s"]
    evidence = _run(controller, "focused")
    assert evidence.truncated is True
    assert evidence.status == "pass"
    log = controller.store.read_artifact_text(
        controller.episode_id, evidence.log_artifact)
    assert "truncated at" in log


def test_source_tampering_during_run_invalidates_evidence(tmp_path, state_dir):
    paths = fixture_paths("recovery")
    repo = tmp_path / "tamper"
    shutil.copytree(paths["repo"], repo)
    controller, _ = make_controller(repo, state_dir, [], ticket_file=paths["ticket"])
    controller.start()
    tamper = controller.workspace.root / "tests" / "test_tamper.py"
    tamper.write_text(
        "from pathlib import Path\n\n"
        "def test_tamper():\n"
        "    target = Path('src/counter.py')\n"
        "    target.write_text(target.read_text() + '\\n# tampered\\n')\n"
        "    assert True\n")
    evidence = _run(controller, "focused")
    assert evidence.mutations == ["src/counter.py"]
    assert evidence.status == "incomplete"
    assert "mutated during test run" in (evidence.incomplete_reason or "")


def test_symlink_never_retrievable_nor_patchable(tmp_path):
    repo = tmp_path / "links"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "real.py").write_text("value = 1\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("value = 999\n")
    (repo / "src" / "link.py").symlink_to(outside)
    from local_coder.retrieval.inventory import Inventory

    inventory = Inventory.build(repo)
    assert "src/real.py" in inventory.files
    assert "src/link.py" not in inventory.files
    dest = tmp_path / "cand"
    ws = CandidateWorkspace.create(repo, dest)
    diff = ("--- a/src/link.py\n+++ b/src/link.py\n@@ -1 +1 @@\n-value = 999\n+value = 0\n")
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(diff, {"src/link.py"}, ["src/**/*.py"], 32 * 1024)
    assert exc.value.code in ("forbidden_path", "missing_file")


def test_multi_file_patch_is_all_or_nothing(tmp_path, state_dir):
    paths = fixture_paths("multi_hunk")
    repo = tmp_path / "partial"
    shutil.copytree(paths["repo"], repo)
    controller, _ = make_controller(repo, state_dir, [], ticket_file=paths["ticket"])
    controller.start()
    ws = controller.workspace
    before = (ws.root / "src/pricing.py").read_bytes()
    gold = (paths["repo"].parent / "gold.diff").read_text()
    head, _, tail = gold.partition("@@ -27,7 +27,7 @@")
    assert "@@" in head and "@@" in "@@ -27,7 +27,7 @@" + tail
    corrupted_tail = tail.replace(
        '     """Return the order total after discount, before tax."""',
        '     """WRONG CONTEXT HERE."""', 1)
    mixed = head + "@@ -27,7 +27,7 @@" + corrupted_tail
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(mixed, {"src/pricing.py"}, ["src/**/*.py"], 32 * 1024)
    assert exc.value.code == "hunk_mismatch"
    assert (ws.root / "src/pricing.py").read_bytes() == before


def test_state_dir_inside_repo_is_refused(tmp_path):
    from .conftest import FIXTURE_REPO

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo)
    controller, _ = make_controller(repo, repo / "state", [])
    with pytest.raises(RuntimeError, match="outside the source checkout"):
        controller.start()


def test_candidate_manifest_ignores_runner_byproducts(tmp_path):
    repo = tmp_path / "byprod"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / ".coverage").write_text("binary-ish\n")
    manifest = candidate_manifest(repo)
    assert "src/a.py" in manifest
    assert ".coverage" not in manifest
