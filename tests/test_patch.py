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


def _replace_controller(repo_copy, state_dir, ws=None):
    """Controller-backed state with one focus file bound to live bytes."""
    from local_coder.paths import sha256_file
    from local_coder.state import FocusRange

    from .conftest import make_controller

    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    workspace = ws or controller.workspace
    controller.state.focus = [FocusRange(path="src/calc.py", start=1, end=9,
                                         sha256=sha256_file(workspace.root / "src/calc.py"),
                                         reason="test")]
    return controller, workspace


def _replace_observation(controller, workspace, edits, episode="test-ep"):
    from local_coder.paths import sha256_file
    from local_coder.persistence import EpisodeStore

    resolved = []
    file_hashes = {}
    for file_id, old, new in edits:
        path = controller.state.focus[file_id - 1].path
        resolved.append((path, old, new))
        file_hashes[path] = sha256_file(workspace.root / path)
    store = EpisodeStore(controller.store.state_dir)
    store.create(episode)
    return patch_skill.run_patch_replace(
        controller.state, workspace, store.artifacts_dir(episode),
        resolved, file_hashes, ["src/**/*.py"], episode, store.write_artifact_text)


def test_replace_unique_succeeds(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    before = (ws.root / "src/calc.py").read_bytes()
    observation, record, reverse, rendered = _replace_observation(
        controller, ws, [(1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b")])
    assert observation["status"] == "applied"
    assert record is not None and reverse is not None and record.files == ["src/calc.py"]
    assert b"return a + b" in (ws.root / "src/calc.py").read_bytes()
    assert b"return a - b" not in (ws.root / "src/calc.py").read_bytes()
    assert before != (ws.root / "src/calc.py").read_bytes()
    assert "-    return a - b" in rendered and "+    return a + b" in rendered
    assert observation["diff_rendered_by"] == "runtime"


def test_replace_missing_and_ambiguous_fail_clean(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    before = (ws.root / "src/calc.py").read_bytes()
    observation, record, reverse, _ = _replace_observation(controller, ws, [(1, "no such line", "x = 1")])
    assert observation["status"] == "rejected" and observation["code"] == "old_text_missing"
    assert record is None and reverse is None
    observation, record, reverse, _ = _replace_observation(
        controller, ws, [(1, "    return a * b", "    return a * b  # dup")])
    assert observation["status"] == "applied"
    assert (ws.root / "src/calc.py").read_bytes() != before


def test_replace_ambiguous_rejected(repo_copy, state_dir, tmp_path):
    target = repo_copy / "src" / "dup.py"
    target.write_text("x = 1\nx = 1\n")
    controller, ws = _replace_controller(repo_copy, state_dir)
    from local_coder.paths import sha256_file
    from local_coder.state import FocusRange

    controller.state.focus.append(FocusRange(path="src/dup.py", start=1, end=2,
                                             sha256=sha256_file(ws.root / "src/dup.py"),
                                             reason="test"))
    before = (ws.root / "src/dup.py").read_bytes()
    observation, record, reverse, _ = _replace_observation(controller, ws, [(2, "x = 1", "x = 2")])
    assert observation["status"] == "rejected" and observation["code"] == "old_text_ambiguous"
    assert record is None
    assert (ws.root / "src/dup.py").read_bytes() == before


def test_replace_whitespace_and_newlines_preserved(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    observation, record, reverse, rendered = _replace_observation(
        controller, ws, [(1, '    """Return the sum of a and b."""',
                          '    """Return the sum of a and b."""\n    # trailing note')])
    assert observation["status"] == "applied"
    text = (ws.root / "src/calc.py").read_text()
    assert '    """Return the sum of a and b."""\n    # trailing note\n' in text
    assert "\r" not in text
    assert "no-op" not in observation.get("code", "")


def test_replace_multiple_edits_one_file(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    observation, record, reverse, _ = _replace_observation(controller, ws, [
        (1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b"),
        (1, "    return a * b", "    return a * b  # product"),
    ])
    assert observation["status"] == "applied"
    assert record is not None and record.files == ["src/calc.py"]
    text = (ws.root / "src/calc.py").read_text()
    assert "return a + b" in text and "# product" in text


def test_replace_across_files(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    from local_coder.paths import sha256_file
    from local_coder.state import FocusRange

    controller.state.focus.append(FocusRange(path="src/__init__.py", start=1, end=1,
                                             sha256=sha256_file(ws.root / "src/__init__.py"),
                                             reason="test"))
    observation, record, reverse, _ = _replace_observation(controller, ws, [
        (1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b"),
        (2, '"""Demo calculator package."""', '"""Demo calculator package v2."""'),
    ])
    assert observation["status"] == "applied"
    assert record is not None and sorted(record.files) == ["src/__init__.py", "src/calc.py"]
    assert "v2" in (ws.root / "src/__init__.py").read_text()


def test_replace_overlapping_rejected(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    before = (ws.root / "src/calc.py").read_bytes()
    observation, record, reverse, _ = _replace_observation(controller, ws, [
        (1, "return a - b", "return a + b"),
        (1, "a - b  # BUG", "a + b  # FIXED"),
    ])
    assert observation["status"] == "rejected" and observation["code"] == "overlapping_edits"
    assert record is None
    assert (ws.root / "src/calc.py").read_bytes() == before


def test_replace_stale_path_symlink_and_forbidden(repo_copy, state_dir, tmp_path):
    from local_coder.workspace import CandidateWorkspace

    controller, ws = _replace_controller(repo_copy, state_dir)
    before = (ws.root / "src/calc.py").read_bytes()
    # Stale: mutate the file after the binding was snapshotted.
    (ws.root / "src/calc.py").write_text((ws.root / "src/calc.py").read_text() + "\n# drift\n")
    stale_hash = controller.state.focus[0].sha256
    observation, record, reverse, _ = _replace_observation(controller, ws, [
        (1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b")])
    assert observation["status"] == "rejected" and observation["code"] == "stale_evidence"
    # Direct workspace-level rejections for escape / symlink / read-only paths.
    dest = tmp_path / "candidate"
    ws2 = CandidateWorkspace.create(repo_copy, dest)
    (ws2.root / "evil-link.py").symlink_to("/etc/hostname")
    h = {p: "0" * 64 for p in ("../evil.py", "src/evil-link.py", "tests/test_calc.py")}
    for rel in ("../evil.py", "src/evil-link.py", "tests/test_calc.py"):
        try:
            ws2.apply_replacements([(rel, "x", "y")], h, {rel}, ["src/**/*.py"], 32 * 1024)
        except Exception as exc:  # noqa: BLE001 - asserting on code below
            assert getattr(exc, "code", "") in ("forbidden_path", "not_in_focus", "missing_file"), rel
        else:
            raise AssertionError(f"{rel} was not rejected")
    assert (ws.root / "src/calc.py").read_bytes() != before  # only our own drift write
    assert stale_hash != "0" * 64


def test_replace_invalid_python_rejected(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    before = (ws.root / "src/calc.py").read_bytes()
    observation, record, reverse, _ = _replace_observation(
        controller, ws, [(1, "    return a - b  # BUG: subtracts instead of adding", "    return a + (")])
    assert observation["status"] == "rejected" and observation["code"] == "syntax_error"
    assert record is None
    assert (ws.root / "src/calc.py").read_bytes() == before


def test_replace_crash_cannot_partially_apply(repo_copy, state_dir, tmp_path, monkeypatch):
    import local_coder.workspace as workspace_mod

    controller, ws = _replace_controller(repo_copy, state_dir)
    before = (ws.root / "src/calc.py").read_bytes()
    real_replace = workspace_mod.os.replace
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(workspace_mod.os, "replace", boom)
    try:
        observation, record, reverse, _ = _replace_observation(controller, ws, [
            (1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b")])
    finally:
        monkeypatch.setattr(workspace_mod.os, "replace", real_replace)
    assert calls, "write path must have been attempted"
    assert record is None
    assert observation["status"] == "rejected"
    assert (ws.root / "src/calc.py").read_bytes() == before


def test_replace_revert_roundtrip(repo_copy, state_dir, tmp_path):
    controller, ws = _replace_controller(repo_copy, state_dir)
    original = (ws.root / "src/calc.py").read_bytes()
    observation, record, reverse, _ = _replace_observation(
        controller, ws, [(1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b")])
    assert observation["status"] == "applied"
    controller.state.last_patch = record
    done = patch_skill.run_patch_revert(controller.state, ws, reverse, "regression")
    assert done["status"] == "reverted"
    assert (ws.root / "src/calc.py").read_bytes() == original


def test_replace_rendered_diff_reproduces_changes(repo_copy, state_dir, tmp_path):
    import subprocess

    controller, ws = _replace_controller(repo_copy, state_dir)
    observation, record, reverse, rendered = _replace_observation(
        controller, ws, [(1, "    return a - b  # BUG: subtracts instead of adding", "    return a + b")])
    assert observation["status"] == "applied"
    assert rendered.startswith("--- a/src/calc.py")
    replay = tmp_path / "replay"
    (replay / "src").mkdir(parents=True)
    (replay / "src" / "calc.py").write_bytes(reverse["src/calc.py"])
    proc = subprocess.run(["git", "apply", "-v"], input=rendered.encode(),
                          cwd=replay, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    assert (replay / "src" / "calc.py").read_bytes() == (ws.root / "src/calc.py").read_bytes()
