"""Prompt 2 FIND: ignore rules, allow/deny policy, gold-label recall."""
import json

from local_coder.retrieval.inventory import Inventory
from local_coder.retrieval.lexical import literal_search, tokenize
from local_coder.retrieval.python_symbols import index_symbols, match_symbols
from local_coder.retrieval.ranking import rank

from .conftest import fixture_paths

QUERIES = {
    "demo_calc": ("add subtracts calc", ["add"]),
    "localization": ("refresh expired session redirect_uri", ["refresh_session"]),
    "multi_hunk": ("order totals quantity discount", ["line_total", "grand_total"]),
    "recovery": ("counter increment reset", ["Counter", "increment"]),
}


def search(repo, query, symbols):
    inventory = Inventory.build(repo)
    lex = literal_search(repo, inventory.files, tokenize(query))
    index = index_symbols(repo, inventory.python_files())
    syms = match_symbols(index, symbols)
    return rank(repo, lex, syms, [])


def recall_at(hits, gold_files, k):
    top = [h.path for h in hits[:k]]
    return sum(1 for f in gold_files if f in top) / max(1, len(gold_files))


def test_recall_at_1_and_3_for_labeled_fixtures(tmp_path):
    import shutil

    for name, (query, symbols) in QUERIES.items():
        paths = fixture_paths(name)
        repo = tmp_path / name
        shutil.copytree(paths["repo"], repo)
        labels = json.loads(paths["labels"].read_text())
        hits = search(repo, query, symbols)
        assert hits, f"{name}: expected retrieval evidence"
        assert recall_at(hits, labels["files"], 1) == 1.0, (
            f"{name}: Recall@1 failed, top hit is {hits[0].path}")
        assert recall_at(hits, labels["files"], 3) == 1.0, f"{name}: Recall@3 failed"


def test_localization_beats_distractors(tmp_path):
    import shutil

    paths = fixture_paths("localization")
    repo = tmp_path / "loc"
    shutil.copytree(paths["repo"], repo)
    hits = search(repo, *QUERIES["localization"])
    assert hits[0].path == "src/session.py"
    assert any("refresh_session" in reason for reason in hits[0].reasons)
    assert all(h.path in ("src/session.py", "src/auth.py", "src/retry.py",
                          "src/cache.py", "tests/test_session.py",
                          "conftest.py", "local-coder.toml") for h in hits)


def test_gitignore_rules_respected(tmp_path):
    repo = tmp_path / "ignored"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "keep.py").write_text("needle = 1\n")
    (repo / "src" / "skip.py").write_text("needle = 2\n")
    (repo / ".gitignore").write_text("src/skip.py\n")
    inventory = Inventory.build(repo)
    assert "src/keep.py" in inventory.files
    assert "src/skip.py" not in inventory.files


def test_allow_deny_policy(tmp_path):
    repo = tmp_path / "policy"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("needle = 1\n")
    (repo / "src" / "b.py").write_text("needle = 2\n")
    denied = Inventory.build(repo, deny=["src/b.py"]).files
    assert "src/a.py" in denied and "src/b.py" not in denied
    allowed = Inventory.build(repo, allow=["src/a.py"]).files
    assert allowed == ["src/a.py"]


def test_hits_carry_line_numbers_and_hashes(tmp_path):
    import shutil

    paths = fixture_paths("recovery")
    repo = tmp_path / "rec"
    shutil.copytree(paths["repo"], repo)
    hits = search(repo, *QUERIES["recovery"])
    assert hits
    for hit in hits[:3]:
        assert hit.start >= 1 and hit.end >= hit.start
        assert len(hit.file_sha) == 64
        assert hit.excerpt and hit.components and hit.reasons
