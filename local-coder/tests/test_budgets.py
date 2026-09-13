"""Bounded execution: eight slots, ninth refused, every attempt accounted."""
import pytest

from local_coder.controller import BudgetExhausted
from local_coder.inference.fake import demo_script

from .conftest import make_controller


def distinct_finds(n):
    return [f'{{"action":"find","query":"distinct-query-{i}","symbols":[]}}' for i in range(n)]


def test_eight_generations_then_blocked_without_ninth_call(repo_copy, state_dir):
    controller, adapter = make_controller(repo_copy, state_dir, distinct_finds(9) + [
        '{"action":"done","status":"blocked","reason_code":"never","summary":"never"}'])
    state = controller.run()
    assert state.generations_used == 8
    assert adapter.calls == 8
    assert state.outcome == "blocked"
    assert "budget exhausted" in (state.stop_reason or "")
    # A further step never touches the model again.
    assert controller.step() is False
    assert adapter.calls == 8


def test_consume_generation_refuses_ninth_without_model_call(repo_copy, state_dir):
    controller, adapter = make_controller(repo_copy, state_dir, [])
    controller.start()
    for _ in range(8):
        controller._consume_generation()
    assert controller.state.generations_used == 8
    with pytest.raises(BudgetExhausted):
        controller._consume_generation()
    assert adapter.calls == 0


def test_malformed_response_consumes_slot_and_warns(repo_copy, state_dir):
    controller, adapter = make_controller(
        repo_copy, state_dir,
        ["not json at all",
         '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'])
    controller.start()
    assert controller.step() is True
    assert controller.state.generations_used == 1
    assert controller.state.consecutive_invalid == 1
    assert controller.last_observation["status"] == "protocol_error"


def test_repeated_invalid_output_terminates_cleanly(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, ["{{{", "[[[",
        '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'])
    controller.start()
    assert controller.step() is True
    assert controller.step() is False
    assert controller.state.outcome == "blocked"
    assert controller.state.generations_used == 2
    assert "invalid" in (controller.state.stop_reason or "")


def test_transport_failure_consumes_slot_with_no_hidden_retry(repo_copy, state_dir):
    controller, adapter = make_controller(
        repo_copy, state_dir,
        ["__TRANSPORT_ERROR__",
         '{"action":"done","status":"blocked","reason_code":"t","summary":"stop"}'])
    controller.start()
    assert controller.step() is True
    assert controller.state.generations_used == 1
    assert adapter.calls == 1
    assert controller.last_observation["status"] == "adapter_error"
    assert controller.step() is False
    assert adapter.calls == 2
    assert controller.state.generations_used == 2


def test_no_hidden_model_calls_on_success_path(repo_copy, state_dir):
    controller, adapter = make_controller(repo_copy, state_dir, demo_script("fake:success"))
    state = controller.run()
    assert state.outcome == "solved"
    assert state.generations_used == 4
    assert adapter.calls == 4


def test_patch_and_check_budgets_reject_without_spending_tests(repo_copy, state_dir):
    bad_patch = '{"action":"patch","operation":"apply","diff":"x","expected_effect":"y"}'
    script = ['{"action":"find","query":"add","symbols":["add"]}', bad_patch, bad_patch]
    controller, _ = make_controller(repo_copy, state_dir, script)
    controller.config.budgets.max_patch_attempts = 1
    controller.start()
    controller.step()  # FIND ok
    controller.step()  # PATCH attempt 1: malformed diff, counts
    assert controller.state.patch_attempts == 1
    before_tests = controller.state.test_invocations
    controller.step()  # PATCH again -> budget rejection, no test spent
    assert controller.state.patch_attempts == 1
    assert controller.state.test_invocations == before_tests
    assert controller.last_observation["code"] == "patch_budget"
