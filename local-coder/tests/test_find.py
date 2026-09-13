"""FIND: real retrieval, exclusions, ranking, focus limits."""
from local_coder.retrieval.inventory import Inventory
from local_coder.retrieval.lexical import literal_search, tokenize
from local_coder.retrieval.python_symbols import index_symbols, match_symbols
from local_coder.retrieval.ranking import rank
from local_coder.skills import find as find_skill

from .conftest import make_controller


def test_recall_top1_with_symbol_evidence(repo_copy):
    inventory = Inventory.build(repo_copy)
    lex = literal_search(repo_copy, inventory.files, tokenize("add subtracts calc"))
    index = index_symbols(repo_copy, inventory.python_files())
    syms = match_symbols(index, ["add"])
    hits = rank(repo_copy, lex, syms, [])
    assert hits, "expected retrieval evidence for the demo bug"
    assert hits[0].path == "src/calc.py"
    assert any("function add" in reason for reason in hits[0].reasons)
    assert hits[0].start == 3 and hits[0].end == 5
    assert len(hits[0].file_sha) == 64


def test_exclusions_hide_secrets_deps_state_and_gold(repo_copy, tmp_path, state_dir):
    (repo_copy / ".env").write_text("OPENAI_KEY=sk-secret add\n")
    (repo_copy / "id_rsa.key").write_text("private add\n")
    git = repo_copy / ".git"
    git.mkdir()
    (git / "index").write_text("add\n")
    (repo_copy / "gold_patch.diff").write_text("add\n")
    hidden_state = tmp_path / "state.json"
    hidden_state.write_text("add\n")
    inventory = Inventory.build(repo_copy)
    assert ".env" not in inventory.files
    assert "id_rsa.key" not in inventory.files
    assert not any(f.startswith(".git/") for f in inventory.files)
    assert "gold_patch.diff" not in inventory.files
    hits = rank(repo_copy, literal_search(repo_copy, inventory.files, ["add"]), [], [])
    assert hits and all(h.path in inventory.files for h in hits)


def test_empty_result_is_honest(repo_copy, state_dir):
    controller, _ = make_controller(
        repo_copy, state_dir,
        ['{"action":"find","query":"zzz-no-such-identifier-zzz","symbols":["NoSuchSymbol"]}'])
    controller.ticket = "zzz qqw xjvk wqbl zorp"  # no overlap with any source token
    controller.start()
    assert controller.step() is True
    assert controller.last_observation["status"] == "empty"
    assert controller.state.focus == []


def test_focus_capped_at_four(tmp_path, state_dir):
    repo = tmp_path / "many"
    (repo / "src").mkdir(parents=True)
    for i in range(6):
        (repo / "src" / f"mod{i}.py").write_text(f"def target_{i}():\n    return {i}\n")
    (repo / "local-coder.toml").write_text("")
    (repo / "conftest.py").write_text("")
    inventory = Inventory.build(repo)
    fresh_total = []
    existing = []
    for i in range(6):
        _, fresh, _ = find_skill.run_find(
            repo, inventory, f"target_{i}", [f"target_{i}"], [], "ticket", 4, existing)
        existing.extend(fresh)
        fresh_total.extend(fresh)
    assert len({f.path for f in fresh_total}) == 4
