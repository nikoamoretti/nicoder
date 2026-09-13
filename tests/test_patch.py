"""PATCH: atomicity, path safety, focus limits, staleness, revert."""
import pytest

from local_coder.inference.fake import DEMO_SUCCESS_DIFF, DEMO_WRONG_DIFF
from local_coder.paths import sha256_file
from local_coder.persistence import EpisodeStore
from local_coder.skills import patch as patch_skill
from local_coder.state import FocusRange
from local_coder.workspace import CandidateWorkspace, PatchValidationError

from .conftest import GOLD_DIFF, make_controller


def make_workspace(repo_copy, tmp_path):
    dest = tmp_path / "candidate"
    return CandidateWorkspace.create(repo_copy, dest)


def focus_for(ws, path="src/calc.py"):
    return {path}


def test_gold_patch_applies_atomically(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    before = (ws.root / "src" / "calc.py").read_bytes()
    result = ws.apply_diff(GOLD_DIFF.read_text(), focus_for(ws), ["src/**/*.py"], 32 * 1024)
    assert result.files == ["src/calc.py"]
    after = (ws.root / "src" / "calc.py").read_text()
    assert "return a + b" in after and "return a - b" not in after
    assert before != (ws.root / "src" / "calc.py").read_bytes()
    leftovers = list(ws.root.rglob("*.patchtmp"))
    assert leftovers == []


def test_fake_success_diff_matches_gold_file():
    assert DEMO_SUCCESS_DIFF == GOLD_DIFF.read_text()


def test_forbidden_paths_rejected(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    before = (ws.root / "src" / "calc.py").read_bytes()
    diff_tests = (
        "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(diff_tests, {"tests/test_calc.py"}, ["src/**/*.py"], 32 * 1024)
    assert exc.value.code == "forbidden_path"
    for bad in ("../evil.py", "/abs/evil.py"):
        diff = f"--- a/{bad}\n+++ b/{bad}\n@@ -1 +1 @@\n-a\n+b\n"
        with pytest.raises(PatchValidationError) as exc:
            ws.apply_diff(diff, {bad.lstrip('/')}, ["**/*.py"], 32 * 1024)
        assert exc.value.code == "forbidden_path"
    assert (ws.root / "src" / "calc.py").read_bytes() == before


def test_creation_deletion_rename_rejected(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    new_file = "--- /dev/null\n+++ b/src/brand_new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(new_file, {"src/brand_new.py"}, ["src/**/*.py"], 32 * 1024)
    assert exc.value.code == "unsupported_operation"
    deletion = "--- a/src/calc.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    with pytest.raises(PatchValidationError):
        ws.apply_diff(deletion, {"src/calc.py"}, ["src/**/*.py"], 32 * 1024)
    rename = "--- a/src/calc.py\n+++ b/src/moved.py\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(PatchValidationError):
        ws.apply_diff(rename, {"src/calc.py"}, ["src/**/*.py"], 32 * 1024)


def test_hunk_mismatch_rejects_without_partial_apply(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    before = (ws.root / "src" / "calc.py").read_bytes()
    bad_context = (
        "--- a/src/calc.py\n+++ b/src/calc.py\n"
        "@@ -2,7 +2,7 @@\n \n def add(a, b):\n"
        '     """WRONG CONTEXT."""\n-    return a - b  # BUG: subtracts instead of adding\n'
        "+    return a + b\n \n \n def mul(a, b):\n"
    )
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(bad_context, focus_for(ws), ["src/**/*.py"], 32 * 1024)
    assert exc.value.code == "hunk_mismatch"
    assert (ws.root / "src" / "calc.py").read_bytes() == before


def test_syntax_error_restores_exact_bytes(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    before = (ws.root / "src" / "calc.py").read_bytes()
    breaking = (
        "--- a/src/calc.py\n+++ b/src/calc.py\n"
        "@@ -2,7 +2,7 @@\n \n def add(a, b):\n"
        '     """Return the sum of a and b."""\n-    return a - b  # BUG: subtracts instead of adding\n'
        "+    return a + (\n \n \n def mul(a, b):\n"
    )
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(breaking, focus_for(ws), ["src/**/*.py"], 32 * 1024)
    assert exc.value.code == "syntax_error"
    assert (ws.root / "src" / "calc.py").read_bytes() == before


def test_patch_size_limit(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff("x" * 100, focus_for(ws), ["src/**/*.py"], 10)
    assert exc.value.code == "patch_too_large"


def test_stale_evidence_rejected_and_revert_roundtrip(repo_copy, state_dir, tmp_path):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    ws = controller.workspace
    controller.state.focus = [FocusRange(path="src/calc.py", start=3, end=5,
                                         sha256=sha256_file(ws.root / "src/calc.py"),
                                         reason="test")]
    observation, record, reverse = patch_skill.run_patch_apply(
        controller.state, ws, tmp_path, DEMO_SUCCESS_DIFF, "fix add",
        ["src/**/*.py"], "test-ep", EpisodeStore(state_dir).write_artifact_text)
    assert observation["status"] == "applied"
    assert record is not None and reverse is not None
    # The controller refreshes focus hashes after an edit; simulate that here.
    controller.state.focus[0].sha256 = sha256_file(ws.root / "src/calc.py")
    assert patch_skill.check_stale(controller.state, ws) == []
    # External mutation invalidates focus: next patch sees stale evidence.
    (ws.root / "src" / "calc.py").write_text((ws.root / "src" / "calc.py").read_text() + "\n# drift\n")
    stale = patch_skill.check_stale(controller.state, ws)
    assert stale == ["src/calc.py"]
    observation2, _, _ = patch_skill.run_patch_apply(
        controller.state, ws, tmp_path, DEMO_WRONG_DIFF, "second",
        ["src/**/*.py"], "test-ep", EpisodeStore(state_dir).write_artifact_text)
    assert observation2["code"] == "stale_evidence"
    # Revert restores the recorded pre-patch bytes (drift blocks unsafe revert).
    controller.state.last_patch = record
    unsafe = patch_skill.run_patch_revert(controller.state, ws, reverse, "regression")
    assert unsafe["code"] == "unsafe_revert"


def test_revert_restores_bytes(repo_copy, state_dir, tmp_path):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    ws = controller.workspace
    original = (ws.root / "src" / "calc.py").read_bytes()
    controller.state.focus = [FocusRange(path="src/calc.py", start=3, end=5,
                                         sha256=sha256_file(ws.root / "src/calc.py"),
                                         reason="test")]
    store = EpisodeStore(state_dir)
    store.create("test-ep")
    observation, record, reverse = patch_skill.run_patch_apply(
        controller.state, ws, tmp_path, DEMO_SUCCESS_DIFF, "fix add",
        ["src/**/*.py"], "test-ep", store.write_artifact_text)
    assert observation["status"] == "applied"
    controller.state.last_patch = record
    done = patch_skill.run_patch_revert(controller.state, ws, reverse, "regression")
    assert done["status"] == "reverted"
    assert (ws.root / "src" / "calc.py").read_bytes() == original


def test_header_body_count_disagreement_rejected_at_parse(repo_copy, tmp_path):
    from local_coder.workspace import CandidateWorkspace

    dest = tmp_path / "candidate"
    ws = CandidateWorkspace.create(repo_copy, dest)
    before = (ws.root / "src" / "calc.py").read_bytes()
    # Live-model-shaped defect: 7-line header over an 8-line body plus a
    # trailing bare marker line. Must fail closed before touching files.
    bad = (
        "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -2,7 +2,7 @@\n \n def add(a, b):\n"
        '     """Return the sum of a and b."""\n-    return a - b  # BUG: subtracts instead of adding\n'
        "+    return a + b  # BUG: subtracts instead of adding\n \n \n def mul(a, b):\n"
        '     """Return the product of a and b."""\n---'
    )
    with pytest.raises(PatchValidationError) as exc:
        ws.apply_diff(bad, {"src/calc.py"}, ["src/**/*.py"], 32 * 1024)
    assert exc.value.code == "malformed_diff"
    assert (ws.root / "src" / "calc.py").read_bytes() == before
