"""Protocol v3 spans: catalogue granularity, editability, transactional apply."""
import hashlib

import pytest

from local_coder import spans as spans_mod
from local_coder.paths import sha256_file
from local_coder.persistence import EpisodeStore
from local_coder.skills import patch as patch_skill
from local_coder.state import FocusRange
from local_coder.workspace import CandidateWorkspace, PatchValidationError

from .conftest import make_controller

EDITABLE = ["src/**/*.py"]


def make_workspace(repo_copy, tmp_path):
    dest = tmp_path / "candidate"
    return CandidateWorkspace.create(repo_copy, dest)


def write_repo(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def catalogue_for(root, focus, budget_chars=20000, editable=EDITABLE):
    return spans_mod.build_catalogue(root, focus, budget_chars, editable=editable)


def _span_of(root, rel, needle):
    data = (root / rel).read_bytes()
    start = data.index(needle)
    return start, start + len(needle)


def _hashes(root, *rels):
    return {rel: sha256_file(root / rel) for rel in rels}


def _apply(ws, span_edits, file_hashes, focus_paths):
    return ws.apply_span_edits(span_edits, file_hashes, focus_paths, EDITABLE, 32 * 1024)


def _text_by_id(root, spans, span_id):
    span = next(s for s in spans if s.span_id == span_id)
    return span, (root / span.path).read_bytes()[span.start_byte:span.end_byte].decode("utf-8")


# -- catalogue granularity ---------------------------------------------------


def test_small_class_offers_each_method_and_children_before_parent(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/svc.py": (
            'class Service:\n'
            '    """Docstring."""\n'
            "\n"
            "    def start(self):\n"
            "        self.running = True\n"
            "\n"
            "    def stop(self):\n"
            "        self.running = False\n"
        ),
    })
    spans, omitted, _ = catalogue_for(root, [("src/svc.py", 1, 12)])
    assert omitted == []
    labels = [(s.kind, s.enclosing, s.span_id) for s in spans]
    kinds = [s.kind for s in spans]
    assert "class" in kinds and kinds.count("method") == 2
    # Each method's body statement is offered, and methods precede the class.
    assert labels.index(("class", "module", labels[[k for k, _, _ in labels].index("class")][2]))
    class_pos = next(i for i, (k, _, _) in enumerate(labels) if k == "class")
    method_positions = [i for i, (k, _, _) in enumerate(labels) if k == "method"]
    assert method_positions and max(method_positions) < class_pos, labels
    # Children (assignments) precede their enclosing method.
    assign_positions = [i for i, (k, _, _) in enumerate(labels) if k == "assign"]
    assert assign_positions and min(assign_positions) < max(method_positions), labels
    for span in spans:
        if span.kind == "method":
            assert span.enclosing == "Service"
        if span.kind == "assign":
            assert span.enclosing in ("Service.start", "Service.stop")


def test_method_with_assignments_returns_branches_offers_statement_children(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/flow.py": (
            "def compute(x):\n"
            "    total = x + 1\n"
            "    if x > 10:\n"
            "        total = x * 2\n"
            "    return total\n"
        ),
    })
    spans, omitted, _ = catalogue_for(root, [("src/flow.py", 1, 5)])
    assert omitted == []
    kinds = sorted(s.kind for s in spans)
    assert "function" in kinds
    assert "assign" in kinds
    assert "if" in kinds
    assert "return" in kinds
    # The nested branch body is a child of the `if`, itself a child of the function.
    inner = [s for s in spans if s.kind == "assign" and "x * 2" in _text_by_id(root, spans, s.span_id)[1]]
    assert inner and inner[0].enclosing == "compute"


def test_nested_control_flow_and_exception_bodies(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/guard.py": (
            "def guard(items):\n"
            "    for item in items:\n"
            "        while item > 0:\n"
            "            item -= 1\n"
            "    try:\n"
            "        risky = items[0]\n"
            "    except IndexError:\n"
            "        risky = None\n"
            "    finally:\n"
            "        del risky\n"
        ),
    })
    spans, _, _ = catalogue_for(root, [("src/guard.py", 1, 10)])
    kinds = [s.kind for s in spans]
    for expected in ("function", "for", "while", "try", "assign"):
        assert expected in kinds, (expected, kinds)
    texts = {s.span_id: _text_by_id(root, spans, s.span_id)[1] for s in spans}
    assert any("item -= 1" in t for t in texts.values())
    assert any("risky = None" in t for t in texts.values())
    assert any("del risky" in t for t in texts.values())


def test_multiline_statement_and_unicode_span(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/u.py": (
            "# héllo wörld 🎉\n"
            "def target():\n"
            "    return (\n"
            '        "naïve" +\n'
            '        "🎉"\n'
            "    )\n"
        ),
    })
    spans, _, _ = catalogue_for(root, [("src/u.py", 1, 6)])
    returns = [s for s in spans if s.kind == "return"]
    assert returns, "multiline return must be an addressable span"
    text = _text_by_id(root, spans, returns[0].span_id)[1]
    assert text.startswith("    return (")
    assert '"🎉"' in text
    assert text.encode("utf-8") == (root / "src/u.py").read_bytes()[
        returns[0].start_byte:returns[0].end_byte]


def test_comments_and_decorators_stay_outside_spans(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/deco.py": (
            "# module comment\n"
            "import functools\n"
            "\n"
            "\n"
            "@functools.cache\n"
            "def cached(x):\n"
            "    # inner comment\n"
            "    return x + 1\n"
        ),
    })
    spans, _, _ = catalogue_for(root, [("src/deco.py", 1, 8)])
    fn = next(s for s in spans if s.kind == "function")
    text = _text_by_id(root, spans, fn.span_id)[1]
    assert text.startswith("def cached(x):")
    assert "@functools.cache" not in text
    assert "# module comment" not in text
    # A child edit must not touch sibling methods or unrelated bytes.
    root2 = write_repo(tmp_path / "repo2", {
        "src/multi.py": (
            "# keep me\n"
            "@decorate\n"
            "def first():\n"
            "    return 1\n"
            "\n"
            "def second():\n"
            "    return 2\n"
        ),
    })
    spans2, _, _ = catalogue_for(root2, [("src/multi.py", 1, 7)])
    child = next(s for s in spans2 if s.kind == "return" and s.enclosing == "first")
    before = (root2 / "src/multi.py").read_bytes()
    ws = CandidateWorkspace(root2)
    start, end = child.start_byte, child.end_byte
    replacement = "    return 10"
    result = _apply(ws, [("src/multi.py", start, end, replacement,
                          hashlib.sha256(before[start:end]).hexdigest())],
                    _hashes(root2, "src/multi.py"), {"src/multi.py"})
    assert result.files == ["src/multi.py"]
    after = (ws.root / "src/multi.py").read_bytes()
    assert after == before[:start] + replacement.encode() + before[end:]
    assert after.startswith(b"# keep me\n@decorate\ndef first():\n")
    assert after.endswith(b"def second():\n    return 2\n")


def test_catalogue_is_deterministic_and_children_survive_trimming(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/big.py": (
            "class Widget:\n"
            + "".join(
                f"    def method_{i}(self):\n        return {i}\n" for i in range(12)
            )
        ),
    })
    focus = [("src/big.py", 1, 30)]
    first, omitted_first, _ = catalogue_for(root, focus)
    second, omitted_second, _ = catalogue_for(root, focus)
    assert [s.span_id for s in first] == [s.span_id for s in second]
    assert omitted_first == omitted_second
    # Budget below the full catalogue drops from the end; the first entries
    # are the small statement children, so the edit-sized units survive.
    trimmed, omitted, _ = spans_mod.build_catalogue(root, focus, 300, editable=EDITABLE)
    assert trimmed, "budget must still allow the smallest units"
    assert omitted, "trimming must record omissions"
    assert [s.span_id for s in trimmed] == [f"S{i}" for i in range(1, len(trimmed) + 1)]
    assert all(s.kind in ("assign", "return", "method") for s in trimmed)
    big_class = [s for s in first if s.kind == "class"]
    assert big_class and big_class[0].span_id not in {s.span_id for s in trimmed}


def test_parent_and_child_spans_are_alternatives_not_overlap_targets(tmp_path):
    root = write_repo(tmp_path / "repo", {
        "src/alt.py": "def f():\n    return 1\n",
    })
    spans, _, _ = catalogue_for(root, [("src/alt.py", 1, 2)])
    fn = next(s for s in spans if s.kind == "function")
    ret = next(s for s in spans if s.kind == "return")
    assert fn.start_byte <= ret.start_byte and ret.end_byte <= fn.end_byte
    ws = CandidateWorkspace(root)
    data = (root / "src/alt.py").read_bytes()
    edits = [
        ("src/alt.py", fn.start_byte, fn.end_byte, "def f():\n    return 2",
         hashlib.sha256(data[fn.start_byte:fn.end_byte]).hexdigest()),
        ("src/alt.py", ret.start_byte, ret.end_byte, "    return 3",
         hashlib.sha256(data[ret.start_byte:ret.end_byte]).hexdigest()),
    ]
    with pytest.raises(PatchValidationError) as exc:
        _apply(ws, edits, _hashes(root, "src/alt.py"), {"src/alt.py"})
    assert exc.value.code == "overlapping_edits"
    assert (ws.root / "src/alt.py").read_bytes() == data


# -- editability filtering ---------------------------------------------------


def test_read_only_paths_get_no_span_bindings(tmp_path):
    root = write_repo(tmp_path, {
        "src/app.py": "def run():\n    return 1\n",
        "tests/test_app.py": "from src.app import run\n\n\ndef test_run():\n    assert run() == 1\n",
        "conftest.py": "import sys\n",
        "local-coder.toml": "version = 1\n",
    })
    focus = [("tests/test_app.py", 1, 5), ("conftest.py", 1, 1),
             ("local-coder.toml", 1, 1), ("src/app.py", 1, 2)]
    spans, omitted, bindings = catalogue_for(root, focus)
    paths = {s.path for s in spans}
    assert paths == {"src/app.py"}
    assert set(bindings) == {s.span_id for s in spans}
    assert all(b["path"] == "src/app.py" for b in bindings.values())
    read_only_notes = [note for note in omitted if "read-only" in note]
    assert len(read_only_notes) == 3
    assert any("local-coder.toml" in note for note in read_only_notes)


def test_read_only_test_remains_visible_as_excerpt_but_not_editable(tmp_path, state_dir):
    from local_coder.briefing import render_full

    from .conftest import make_controller as factory

    spans, omitted, _ = catalogue_for(
        tmp_path, [("tests/test_calc.py", 1, 20)], editable=EDITABLE)
    assert spans == []
    assert any("read-only" in note for note in omitted)
    # Briefing still shows the test file as an excerpt when it is focused.
    repo = tmp_path / "repo"
    import shutil

    from .conftest import FIXTURE_REPO

    shutil.copytree(FIXTURE_REPO, repo)
    controller, _ = factory(repo, state_dir, [], protocol_version="3")
    controller.start()
    controller.state.focus = [FocusRange(path="tests/test_calc.py", start=1, end=13,
                                         sha256=sha256_file(repo / "tests/test_calc.py"),
                                         reason="evidence")]
    controller.workspace.root = repo
    text, spans, _ = render_full(controller.state, controller.workspace, None,
                                 protocol_version="3", editable=EDITABLE)
    assert "tests/test_calc.py:1-13" in text
    assert spans == []
    assert "SPANS" in text and "(no editable spans" in text


def test_symlinks_and_escaping_paths_excluded(tmp_path):
    root = write_repo(tmp_path, {"src/real.py": "value = 1\n"})
    (root / "src" / "link.py").symlink_to("/etc/hostname")
    spans, omitted, _ = catalogue_for(root, [("src/link.py", 1, 1), ("src/real.py", 1, 1)])
    assert [s.path for s in spans] == ["src/real.py"]
    assert any("symlink" in note for note in omitted)
    escaped, escaped_notes, _ = catalogue_for(root, [("../outside.py", 1, 1)],
                                              editable=["**/*.py"])
    assert escaped == []
    assert any("read-only" in note or "escapes" in note for note in escaped_notes)


def test_trimmed_span_cannot_be_addressed(tmp_path):
    from local_coder.briefing import render_full

    repo = tmp_path / "repo"
    import shutil

    from .conftest import FIXTURE_REPO

    shutil.copytree(FIXTURE_REPO, repo)
    state_dir = tmp_path / "episodes"
    controller, _ = make_controller(repo, state_dir, [], protocol_version="3")
    controller.start()
    controller.workspace = CandidateWorkspace(repo)
    from local_coder.state import FocusRange

    controller.state.focus = [FocusRange(path="src/calc.py", start=1, end=12,
                                         sha256=sha256_file(repo / "src/calc.py"),
                                         reason="test")]
    full_spans, _, _ = catalogue_for(repo, [("src/calc.py", 1, 12)])
    assert len(full_spans) >= 2
    dropped_id = full_spans[-1].span_id
    kept_ids = {s.span_id for s in full_spans[:-1]}
    assert dropped_id not in kept_ids
    # Reserve a trimmed catalogue: only kept IDs are persisted and resolvable.
    from local_coder.briefing import Span

    trimmed = [Span(**{k: v for k, v in s.__dict__.items()}) for s in full_spans[:-1]]
    controller._open_turn = 1
    controller.store.create("trimmed")
    controller.episode_id = "trimmed"
    controller.store.write_turn_file("trimmed", 1, "spans.json", __import__("json").dumps([
        {"span_id": s.span_id, "path": s.path, "start_byte": s.start_byte,
         "end_byte": s.end_byte, "file_sha256": s.file_sha256,
         "bytes_sha256": s.bytes_sha256, "kind": s.kind, "enclosing": s.enclosing}
        for s in trimmed]))
    bindings = controller._turn_spans()
    assert bindings is not None
    assert dropped_id not in bindings
    assert set(bindings) == kept_ids
    _ = render_full


# -- transactional application ----------------------------------------------


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
    assert all(b["kind"] for b in bindings.values())
    assert all(b["enclosing"] for b in bindings.values())


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
    assert any(s.kind == "assign" for s in big)
    assert all(s.end_byte - s.start_byte <= spans_mod.MAX_SPAN_BYTES for s in big)


def test_catalogue_budget_and_cap_are_deterministic(tmp_path):
    root = write_repo(tmp_path / "repo", {
        f"src/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(1, 11)
    })
    focus = [(f"src/m{i}.py", 1, 3) for i in range(1, 11)]
    first, omitted_first, _ = spans_mod.build_catalogue(root, focus, 200, editable=EDITABLE)
    second, omitted_second, _ = spans_mod.build_catalogue(root, focus, 200, editable=EDITABLE)
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
            ws.apply_span_edits([(rel, 0, 1, "y", "0" * 64)], h, {rel}, EDITABLE, 32 * 1024)
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
                {"src/one.py", "src/two.py"}, EDITABLE, 32 * 1024)
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
        _hashes(ws.root, "src/calc.py"), EDITABLE, "test-ep", store.write_artifact_text)
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