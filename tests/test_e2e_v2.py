"""Protocol v2 end-to-end: replace flow, bindings, budget accounting."""
import json

from local_coder.export import export_episode

from .conftest import make_controller


def _v2_script(old, new, file_id=1, effect="fix add"):
    return [
        '{"action":"find","query":"add subtracts calc","symbols":["add"]}',
        json.dumps({"action": "patch", "operation": "replace",
                    "edits": [{"file_id": file_id, "old": old, "new": new}],
                    "expected_effect": effect}),
        '{"action":"check","scope":"focused"}',
        '{"action":"done","status":"solved","summary":"Fixed add()."}',
    ]


def test_v2_replace_solves_demo_calc(repo_copy, state_dir):
    script = _v2_script("    return a - b  # BUG: subtracts instead of adding", "    return a + b",
                        file_id=2)
    controller, adapter = make_controller(repo_copy, state_dir, script, episode_id="ep-v2-ok",
                                          protocol_version="2")
    state = controller.run()
    assert state.outcome == "solved", state.stop_reason
    assert state.generations_used == 4
    assert adapter.calls == 4
    assert state.provenance["protocol_version"] == "2"
    assert [f.path for f in state.focus][:2] == ["tests/test_calc.py", "src/calc.py"]
    assert (controller.workspace.root / "src/calc.py").read_text().find("return a + b") >= 0
    records = export_episode(controller.store, "ep-v2-ok", split="dev")
    assert all(r["protocol_version"] == "2" for r in records if r["complete"])
    assert all(r["constraint"] == "json_object" for r in records if r["complete"])
    assert any(r.get("verified_transition") for r in records)


def test_v2_unknown_file_id_rejected(repo_copy, state_dir):
    script = _v2_script("    return a - b", "    return a + b", file_id=9) + [
        '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}']
    controller, _ = make_controller(repo_copy, state_dir, script[:3] + script[4:],
                                    episode_id="ep-v2-unknown", protocol_version="2")
    controller.start()
    controller.step()
    controller.step()
    assert controller.last_observation["status"] == "rejected"
    assert controller.last_observation["code"] == "unknown_file_id"
    assert controller.state.patch_attempts == 1


def test_v2_replace_action_under_v1_is_protocol_error(repo_copy, state_dir):
    script = _v2_script("    return a - b", "    return a + b")
    controller, _ = make_controller(repo_copy, state_dir, script, episode_id="ep-v1-replace",
                                    protocol_version="1")
    controller.start()
    assert controller.step() is True
    assert controller.step() is True
    assert controller.last_observation["status"] == "protocol_error"
    assert controller.last_observation["code"] == "unsupported_operation"


def test_v2_wrong_protocol_patch_rejected_without_apply(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [], episode_id="ep-v2-guard",
                                    protocol_version="1")
    controller.start()
    assert controller._open_turn is None
