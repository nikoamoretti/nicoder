"""Protocol v3 briefing: span display, IDs, constraints, legend hygiene."""
from local_coder import briefing as briefing_mod

from .conftest import make_controller

EDITABLE = ["src/**/*.py"]


def _started(repo_copy, state_dir, protocol_version="3"):
    controller, _ = make_controller(
        repo_copy, state_dir,
        ['{"action":"find","query":"add subtracts calc","symbols":["add"]}'],
        protocol_version=protocol_version)
    controller.start()
    controller.step()
    return controller


def test_v3_briefing_shows_spans_and_schema(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    text, spans, omitted = briefing_mod.render_full(
        controller.state, controller.workspace, None, protocol_version="3",
        editable=EDITABLE)
    assert spans, "v3 must offer editable spans for a focused source file"
    assert all(s.path.startswith("src/") for s in spans)
    assert '"operation":"replace_span"' in text
    assert "SPANS" in text
    for span in spans:
        assert f"[{span.span_id}]" in text
    assert "Never reproduce old text" in text
    assert any("read-only" in note for note in omitted), \
        "the focused test file must be recorded as non-editable"
    # Displayed span text carries no injected line-number prefixes and is the
    # exact byte slice of the source file.
    first_block = text.split("[S1]", 1)[1].split("\n", 1)[1].split("\n[S", 1)[0]
    assert not first_block.splitlines()[0].startswith(("1:", "2:", "3:"))
    data = (controller.workspace.root / spans[0].path).read_bytes()
    assert data[spans[0].start_byte:spans[0].end_byte].decode("utf-8") in first_block
    assert "kind=" in text and "enclosing=" in text


def test_v3_span_ids_match_bindings_and_reserve(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    text, spans, _ = briefing_mod.render_full(
        controller.state, controller.workspace, None, protocol_version="3",
        editable=EDITABLE)
    turn = controller._reserve_slot(text, controller.adapter.token_counter(), 1, "ESTIMATE",
                                    "sha", 1, spans=spans)
    assert turn == 2
    raw = controller.store.read_turn_file(controller.episode_id, 2, "spans.json")
    import json

    persisted = json.loads(raw)
    assert [p["span_id"] for p in persisted] == [s.span_id for s in spans]
    assert all(p["file_sha256"] == s.file_sha256 for p, s in zip(persisted, spans, strict=True))
    assert all(p["kind"] and p["enclosing"] for p in persisted)
    profile = json.loads(controller.store.read_turn_file(controller.episode_id, 2, "profile.json"))
    assert profile["catalogue"] == "span-catalogue-v2"


def test_v3_example_is_task_independent(repo_copy, state_dir):
    lowered = briefing_mod.V3_SPAN_EXAMPLE.lower()
    for token in ("calc", "session", "redirect", "pricing", "counter",
                  "gold", "held-out", "fixture"):
        assert token not in lowered, token
    assert "src/toy.py" in briefing_mod.V3_SPAN_EXAMPLE


def test_v3_constraints_advertise_exhausted_budgets(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    state = controller.state
    assert briefing_mod.constraint_notes(state) == []
    state.patch_attempts = state.budgets.max_patch_attempts
    notes = briefing_mod.constraint_notes(state)
    assert any("PATCH unavailable" in note for note in notes)
    text, _, _ = briefing_mod.render_full(state, controller.workspace, None, protocol_version="3")
    assert "CONSTRAINTS" in text and "PATCH unavailable" in text
    state.model_checks = state.budgets.max_model_checks
    notes = briefing_mod.constraint_notes(state)
    assert any("CHECK unavailable" in note for note in notes)
