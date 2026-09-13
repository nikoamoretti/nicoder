"""Transitions, briefing, config/CLI surface, live-adapter honesty."""
import pytest

from local_coder import briefing as briefing_mod
from local_coder.config import EXAMPLE_TOML, load_config
from local_coder.controller import TRANSITIONS, allowed_actions
from local_coder.inference.llama_cpp import AdapterUnavailable, LlamaCppAdapter, check_configured

from .conftest import make_controller


def test_transition_table_covers_phases():
    assert allowed_actions("INIT") == set()
    assert allowed_actions("TERMINAL") == set()
    for phase in ("DISCOVER", "EDIT", "VERIFY", "RECOVER"):
        assert allowed_actions(phase) == {"find", "patch", "check", "done"}
    assert TRANSITIONS[("INIT", "baseline_ok")] == "DISCOVER"
    assert TRANSITIONS[("VERIFY", "check_regression")] == "RECOVER"
    assert TRANSITIONS[("RECOVER", "check_clean")] == "VERIFY"
    assert TRANSITIONS[("EDIT", "check")] == "VERIFY"


def test_briefing_regenerates_without_growing(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    first = briefing_mod.render(controller.state, controller.workspace, None)
    controller.last_observation = {"status": "ok", "hits": []}
    second = briefing_mod.render(controller.state, controller.workspace, controller.last_observation)
    assert "TASK" in second and "RULES" in second
    assert second.count("LAST OBSERVATION") == 1
    assert len(second) < len(first) + 2000
    assert "HYPOTHESIS (model proposal, not a fact)" in second


def test_briefing_context_budget_exceeded(repo_copy, state_dir):
    controller, _ = make_controller(repo_copy, state_dir, [])
    controller.start()
    controller.state.budgets.max_prompt_tokens = 10
    with pytest.raises(briefing_mod.ContextBudgetExceeded):
        briefing_mod.render(controller.state, controller.workspace, None)


def test_init_and_doctor_do_not_generate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")

    class Args:
        pass

    from local_coder import cli as cli_mod

    init_args = Args()
    init_args.repo = str(repo)
    init_args.force = False
    assert cli_mod.cmd_init(init_args) == 0
    assert (repo / "local-coder.toml").exists()
    config = load_config(repo)
    assert "focused" in config.commands
    # init twice without --force refuses to overwrite.
    assert cli_mod.cmd_init(init_args) == 1
    doctor_args = Args()
    doctor_args.repo = str(repo)
    doctor_args.probe_model = False
    assert cli_mod.cmd_doctor(doctor_args) == 0


def test_example_toml_parses():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "local-coder.toml").write_text(EXAMPLE_TOML)
        config = load_config(repo)
        assert config.execution in ("isolated", "trusted-local")
        assert set(config.commands) >= {"baseline", "focused", "regression", "final"}


def test_llama_adapter_rejects_non_loopback():
    from local_coder.inference.base import AdapterError

    with pytest.raises(AdapterError):
        LlamaCppAdapter("https://example.com/v1")


def test_live_tests_marked_not_run_without_server():
    with pytest.raises(AdapterUnavailable):
        check_configured(None)
