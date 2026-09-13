"""Protocol v3 spans: catalogue safety and transactional application."""
import hashlib

import pytest

from local_coder import spans as spans_mod
from local_coder.paths import sha256_file
from local_coder.persistence import EpisodeStore
from local_coder.skills import patch as patch_skill
from local_coder.state import FocusRange
from local_coder.workspace import CandidateWorkspace, PatchValidationError

from .conftest import make_controller


def make_workspace(repo_copy, tmp_path):
    dest = tmp_path / "candidate"
    return CandidateWorkspace.create(repo_copy, dest)


def write_repo(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def catalogue_for(root, focus, budget_chars=20000):
    spans, omitted, bindings = spans_mod.build_catalogue(root, focus, budget_chars)
    return spans, omitted, bindings


def _span_of(root, rel, needle):
    data = (root / rel).read_bytes()
    start = data.index(needle)
    return start, start + len(needle)


def _hashes(root, *rels):
    return {rel: sha256_file(root / rel) for rel in rels}


def _apply(ws, span_edits, file_hashes, focus_paths):
    return ws.apply_span_edits(span_edits, file_hashes, focus_paths, ["src/**/*.py"], 32 * 1024)


def test_catalogue_prefers_statements(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    spans, omitted, bindings = catalogue_for(ws.root, [("src/calc.py", 1, 12)])
    assert omitted == []
    assert [s.span_id for s in spans] == [f"S{i}" for i in range(1, len(spans) + 1)]
    data = (ws.root / "src/calc.py").read_bytes()
    assert all(s.path == "src/calc.py" for s in spans)
    assert all(data[s.start_byte:s.end_byte].decode("utf-8").strip() for s in spans)
    assert spans[0].start_byte == 0
    assert spans[0].bytes_sha256 == hashlib.sha256(
        data[spans[0].start_byte:spans[0].end_byte]).hexdigest()
    assert spans[0].file_sha256 == sha256_file(ws.root / "src/calc.py")
    assert set(bindings) == {s.span_id for s in spans}
    assert bindings["S1"]["start_byte"] == str(spans[0].start_byte)


def test_catalogue_only_covers_excerpt_range(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    spans, _, _ = catalogue_for(ws.root, [("src/calc.py", 4, 6)])
    assert spans, "expected spans overlapping lines 4-6"
    data = (ws.root / "src/calc.py").read_bytes()
    for span in spans:
        text = data[span.start_byte:span.end_byte].decode("utf-8")
        assert "def add" in text or "return a - b" in text or "sum of a and b" in text


def test_catalogue_omits_unparseable_and_oversized(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/broken.py": "def oops(:\n",
        "src/big.py": "VALUE = 1\n\ndef huge():\n" + "".join(f"    step_{i} = {i}\n" for i in range(200)),
    })
    spans, omitted, _ = catalogue_for(
        root, [("src/broken.py", 1, 5), ("src/big.py", 1, 300)], budget_chars=20000)
    assert any("unparseable" in note for note in omitted)
    assert not [s for s in spans if s.path == "src/broken.py"]
    # The oversized function descends into child statements, each addressable.
    big = [s for s in spans if s.path == "src/big.py"]
    assert big
    assert all(s.end_byte - s.start_byte <= spans_mod.MAX_SPAN_BYTES for s in big)


def test_catalogue_budget_and_cap_are_deterministic(tmp_path):
    root = write_repo(tmp_path / "repo", {
        f"src/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(1, 11)
    })
    focus = [(f"src/m{i}.py", 1, 3) for i in range(1, 11)]
    first, omitted_first, _ = spans_mod.build_catalogue(root, focus, 200)
    second, omitted_second, _ = spans_mod.build_catalogue(root, focus, 200)
    assert [(s.span_id, s.path) for s in first] == [(s.span_id, s.path) for s in second]
    assert omitted_first == omitted_second
    assert omitted_first, "tiny budget must omit trailing spans"
    kept = {s.span_id for s in first}
    assert kept == {f"S{i}" for i in range(1, len(first) + 1)}, "survivors keep leading IDs"


def test_span_unique_targeting_despite_duplicates(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/dup.py": "def first():\n    x = 1\n    return x\n\ndef second():\n    x = 1\n    return x\n",
    })
    ws = CandidateWorkspace(root)
    data = (ws.root / "src/dup.py").read_bytes()
    needle = b"    x = 1"
    start = data.index(needle)  # first occurrence only
    result = _apply(ws, [("src/dup.py", start, start + len(needle), "    x = 2",
                          hashlib.sha256(needle).hexdigest())],
                    _hashes(ws.root, "src/dup.py"), {"src/dup.py"})
    assert result.files == ["src/dup.py"]
    text = (ws.root / "src/dup.py").read_text()
    assert text.count("    x = 2") == 1 and text.count("    x = 1") == 1


def test_span_stale_file_and_slice_rejected(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    data = (ws.root / "src/calc.py").read_bytes()
    start, end = _span_of(ws.root, "src/calc.py", b"    return a - b")
    with pytest.raises(PatchValidationError) as exc:
        _apply(ws, [("src/calc.py", start, end, "    return a + b", "0" * 64)],
               _hashes(ws.root, "src/calc.py"), {"src/calc.py"})
    assert exc.value.code == "stale_span"
    with pytest.raises(PatchValidationError) as exc:
        _apply(ws, [("src/calc.py", start, end, "    return a + b",
                     hashlib.sha256(data[start:end]).hexdigest())],
               {"src/calc.py": "0" * 64}, {"src/calc.py"})
    assert exc.value.code == "stale_evidence"
    assert (ws.root / "src/calc.py").read_bytes() == data


def test_span_multiple_disjoint_edits_one_action(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    data = (ws.root / "src/calc.py").read_bytes()
    s1, e1 = _span_of(ws.root, "src/calc.py", b"    return a - b")
    s2, e2 = _span_of(ws.root, "src/calc.py", b"    return a * b")
    h = lambda s, e: hashlib.sha256(data[s:e]).hexdigest()  # noqa: E731
    result = _apply(ws, [("src/calc.py", s1, e1, "    return a + b", h(s1, e1)),
                         ("src/calc.py", s2, e2, "    return a * b  # product", h(s2, e2))],
                    _hashes(ws.root, "src/calc.py"), {"src/calc.py"})
    assert result.files == ["src/calc.py"]
    text = (ws.root / "src/calc.py").read_text()
    assert "return a + b" in text and "# product" in text


def test_span_overlap_rejected(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    data = (ws.root / "src/calc.py").read_bytes()
    before = bytes(data)
    s1, e1 = _span_of(ws.root, "src/calc.py", b"return a - b")
    s2, e2 = _span_of(ws.root, "src/calc.py", b"a - b  # BUG")
    h = lambda s, e: hashlib.sha256(data[s:e]).hexdigest()  # noqa: E731
    with pytest.raises(PatchValidationError) as exc:
        _apply(ws, [("src/calc.py", s1, e1, "X", h(s1, e1)),
                    ("src/calc.py", s2, e2, "Y", h(s2, e2))],
               _hashes(ws.root, "src/calc.py"), {"src/calc.py"})
    assert exc.value.code == "overlapping_edits"
    assert (ws.root / "src/calc.py").read_bytes() == before


def test_span_unicode_byte_offsets(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/u.py": '# héllo wörld 🎉\ns = "naïve"\n\n\ndef target():\n    return 41\n',
    })
    ws = CandidateWorkspace(root)
    data = (ws.root / "src/u.py").read_bytes()
    needle = b"    return 41"
    start = data.index(needle)
    result = _apply(ws, [("src/u.py", start, start + len(needle), "    return 42",
                          hashlib.sha256(needle).hexdigest())],
                    _hashes(ws.root, "src/u.py"), {"src/u.py"})
    assert result.files == ["src/u.py"]
    text = (ws.root / "src/u.py").read_text(encoding="utf-8")
    assert text.startswith('# héllo wörld 🎉\ns = "naïve"')
    assert "return 42" in text
    # Bytes 4-6 split the multibyte é: invalid boundary, never lossy decode.
    with pytest.raises(PatchValidationError) as exc:
        _apply(ws, [("src/u.py", 4, 6, "X", hashlib.sha256(data[4:6]).hexdigest())],
               _hashes(ws.root, "src/u.py"), {"src/u.py"})
    assert exc.value.code == "invalid_span"


def test_span_multiline_and_surroundings_preserved(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    data = (ws.root / "src/calc.py").read_bytes()
    needle = (b'    """Return the sum of a and b."""\n'
              b"    return a - b  # BUG: subtracts instead of adding")
    start = data.index(needle)
    replacement = '    """Return the sum of a and b."""\n    return a + b'
    result = _apply(ws, [("src/calc.py", start, start + len(needle), replacement,
                          hashlib.sha256(needle).hexdigest())],
                    _hashes(ws.root, "src/calc.py"), {"src/calc.py"})
    assert result.files == ["src/calc.py"]
    after = (ws.root / "src/calc.py").read_bytes()
    assert after.startswith(b'"""Tiny arithmetic library used as an offline fixture."""\n\ndef add(a, b):\n')
    assert after.endswith(b'\n\ndef mul(a, b):\n    """Return the product of a and b."""\n    return a * b\n')
    assert b"return a + b" in after and b"return a - b" not in after
    assert b"# BUG" not in after


def test_span_syntax_failure_rejected(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    data = (ws.root / "src/calc.py").read_bytes()
    before = bytes(data)
    start, end = _span_of(ws.root, "src/calc.py", b"    return a - b")
    with pytest.raises(PatchValidationError) as exc:
        _apply(ws, [("src/calc.py", start, end, "    return a + (",
                     hashlib.sha256(data[start:end]).hexdigest())],
               _hashes(ws.root, "src/calc.py"), {"src/calc.py"})
    assert exc.value.code == "syntax_error"
    assert (ws.root / "src/calc.py").read_bytes() == before


def test_span_forbidden_paths_rejected(repo_copy, tmp_path):
    ws = make_workspace(repo_copy, tmp_path)
    h = {"src/calc.py": "0" * 64, "../evil.py": "0" * 64,
         "src/evil-link.py": "0" * 64, "tests/test_calc.py": "0" * 64}
    (ws.root / "src" / "evil-link.py").symlink_to("/etc/hostname")
    for rel in ("../evil.py", "src/evil-link.py", "tests/test_calc.py"):
        with pytest.raises(PatchValidationError) as exc:
            ws.apply_span_edits([(rel, 0, 1, "y", "0" * 64)], h, {rel}, ["src/**/*.py"], 32 * 1024)
        assert getattr(exc.value, "code", "") in ("forbidden_path", "not_in_focus", "missing_file"), rel


def test_span_interrupted_publish_rolls_back(tmp_path):
    import local_coder.workspace as workspace_mod

    root = write_repo(tmp_path / "repo", {
        "src/one.py": "VALUE = 1\n",
        "src/two.py": "VALUE = 2\n",
    })
    ws = CandidateWorkspace(root)
    before_one = (ws.root / "src/one.py").read_bytes()
    before_two = (ws.root / "src/two.py").read_bytes()
    real_replace = workspace_mod.os.replace
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise OSError("simulated crash mid-publish")

    workspace_mod.os.replace = boom
    try:
        with pytest.raises(PatchValidationError) as exc:
            ws.apply_span_edits(
                [("src/one.py", 0, len(before_one), "VALUE = 10\n",
                  hashlib.sha256(before_one).hexdigest()),
                 ("src/two.py", 0, len(before_two), "VALUE = 20\n",
                  hashlib.sha256(before_two).hexdigest())],
                _hashes(ws.root, "src/one.py", "src/two.py"),
                {"src/one.py", "src/two.py"}, ["src/**/*.py"], 32 * 1024)
    finally:
        workspace_mod.os.replace = real_replace
    assert calls, "publish path must have been attempted"
    assert exc.value.code == "write_failed"
    assert (ws.root / "src/one.py").read_bytes() == before_one
    assert (ws.root / "src/two.py").read_bytes() == before_two


def test_span_revert_and_rendered_diff(repo_copy, state_dir, tmp_path):
    import subprocess

    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    ws = controller.workspace
    controller.state.focus = [FocusRange(path="src/calc.py", start=1, end=12,
                                         sha256=sha256_file(ws.root / "src/calc.py"),
                                         reason="test")]
    original = (ws.root / "src/calc.py").read_bytes()
    start, end = _span_of(ws.root, "src/calc.py", b"    return a - b")
    store = EpisodeStore(controller.store.state_dir)
    store.create("test-ep")
    observation, record, reverse, rendered = patch_skill.run_patch_replace_span(
        controller.state, ws, store.artifacts_dir("test-ep"),
        [("src/calc.py", start, end, "    return a + b",
          hashlib.sha256(original[start:end]).hexdigest())],
        _hashes(ws.root, "src/calc.py"), ["src/**/*.py"], "test-ep", store.write_artifact_text)
    assert observation["status"] == "applied"
    assert observation["interface"] == "replace_span"
    assert record is not None and reverse is not None
    controller.state.last_patch = record
    done = patch_skill.run_patch_revert(controller.state, ws, reverse, "regression")
    assert done["status"] == "reverted"
    assert (ws.root / "src/calc.py").read_bytes() == original
    assert rendered.startswith("--- a/src/calc.py")
    replay = tmp_path / "replay"
    (replay / "src").mkdir(parents=True)
    (replay / "src" / "calc.py").write_bytes(original)
    (replay / "check.diff").write_text(rendered)
    proc = subprocess.run(["git", "apply", str(replay / "check.diff")], cwd=replay, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"return a + b" in (replay / "src" / "calc.py").read_bytes()
