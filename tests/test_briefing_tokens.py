"""Prompt 3 briefing budgets: measured vs estimated, deterministic trim."""
import pytest

from local_coder import briefing as briefing_mod
from local_coder.briefing import ContextBudgetExceeded
from local_coder.inference.tokenizer import EstimateCounter

from .conftest import make_controller


class HalfCounter:
    """Deterministic exact counter stub (1 token per 2 chars)."""

    name = "stub-exact"
    exact = True

    def count(self, text):
        return max(1, len(text) // 2)


def _started(repo_copy, state_dir):
    controller, _ = make_controller(
        repo_copy, state_dir,
        ['{"action":"find","query":"add subtracts calc","symbols":["add"]}'])
    controller.start()
    controller.step()
    assert controller.state.focus, "FIND must select focus for trim tests"
    return controller


def test_exceeded_budget_names_counter_kind(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    controller.state.budgets.max_prompt_tokens = 10
    with pytest.raises(ContextBudgetExceeded, match="ESTIMATE"):
        briefing_mod.render(controller.state, controller.workspace, None)
    with pytest.raises(ContextBudgetExceeded, match="MEASURED"):
        briefing_mod.render(controller.state, controller.workspace, None,
                            counter=HalfCounter())


def test_excerpt_trimming_is_deterministic_and_recorded(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    full = briefing_mod.render(controller.state, controller.workspace, None)
    assert len(controller.state.focus) >= 1
    controller.state.budgets.max_prompt_tokens = EstimateCounter().count(full) - 10
    omitted: list = []
    trimmed = briefing_mod.render(controller.state, controller.workspace, None,
                                  omitted_note=omitted)
    assert len(trimmed) < len(full)
    assert omitted, "omissions must be recorded, not silent"
    again = briefing_mod.render(controller.state, controller.workspace, None)
    assert again == trimmed


def test_measured_overhead_replaces_conservative_reserve(repo_copy, state_dir):
    controller = _started(repo_copy, state_dir)
    budgets = controller.state.budgets
    budgets.max_prompt_tokens = 10 ** 9
    budgets.template_reserve_tokens = 10 ** 9
    with pytest.raises(ContextBudgetExceeded):
        briefing_mod.render(controller.state, controller.workspace, None,
                            context_limit=budgets.server_context_tokens)
    controller.state.template_overhead_measured = 0
    text = briefing_mod.render(controller.state, controller.workspace, None,
                               context_limit=budgets.server_context_tokens)
    assert "TASK" in text and "RULES" in text
