"""Protocol v2 briefing: file IDs, schema, task-independent example."""
from local_coder import briefing as briefing_mod

from .conftest import make_controller


def _started(repo_copy, state_dir, protocol_version="2"):
    controller, _ = make_controller(
        repo_copy, state_dir,
        ['{"action":"find","query":"add subtracts calc","symbols":["add"]}'],
        protocol_version=protocol_version)
    controller.start()
    controller.step()
    assert controller.state.focus, "FIND must select focus for briefing tests"
    return controller


def test_v2_briefing_numbers_files_and_shows_replace_schema(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    text = briefing_mod.render(controller.state, controller.workspace, None,
                               protocol_version="2")
    assert "[id=1] tests/test_calc.py" in text
    assert "[id=2] src/calc.py" in text
    assert '"operation":"replace"' in text
    assert "unified diff" not in text.split("RULES")[1].split("PATCH")[0].replace(
        "Never author unified diffs", "")
    assert "Never author unified diffs" in text
    assert "Example replace (toy file only" in text


def test_v2_example_is_task_independent(repo_copy, state_dir):
    assert "src/toy.py" in briefing_mod.V2_EXAMPLE
    lowered = briefing_mod.V2_EXAMPLE.lower()
    for token in ("calc", "session", "redirect", "pricing", "counter",
                  "gold", "held-out", "fixture"):
        assert token not in lowered, token


def test_v1_briefing_has_no_ids_or_example(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir, protocol_version="1")
    text = briefing_mod.render(controller.state, controller.workspace, None,
                               protocol_version="1")
    assert "[id=" not in text
    assert "toy file only" not in text
    assert '"operation":"apply"' in text


def test_focus_bindings_mirror_briefing_order(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    bindings = briefing_mod.focus_bindings(controller.state)
    assert [b["path"] for _, b in sorted(bindings.items())] == [
        f.path for f in controller.state.focus]
    for binding in bindings.values():
        assert len(binding["sha256"]) == 64
    assert set(bindings) == set(range(1, len(controller.state.focus) + 1))
