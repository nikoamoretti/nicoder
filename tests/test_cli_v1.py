"""Usable-v1 CLI surface: setup, transactional accept, profile-safe resume."""
import sys
from types import SimpleNamespace

from local_coder import machine
from local_coder.cli import cmd_accept, cmd_doctor, cmd_init, cmd_resume, cmd_run, cmd_setup
from local_coder.inference.fake import demo_script
from local_coder.paths import sha256_file

from .conftest import FIXTURE_DIR, make_controller


def _solve(repo, state_dir, episode="ep-v1"):
    controller, _ = make_controller(repo, state_dir, demo_script("fake:success"), episode_id=episode)
    state = controller.run()
    assert state.outcome == "solved"
    return controller, state


def test_setup_writes_machine_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    args = SimpleNamespace(model_file="/tmp/model.gguf", model_alias="tiny", endpoint=None,
                           server_binary=None, n_ctx=4096, write=True)
    assert cmd_setup(args) == 0
    cfg = machine.load_machine_config()
    assert cfg.model_file == "/tmp/model.gguf"
    assert cfg.model_alias == "tiny"
    assert cfg.n_ctx == 4096
    assert "wrote machine configuration" in capsys.readouterr().out


def test_setup_without_write_changes_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    args = SimpleNamespace(model_file="/tmp/model.gguf", model_alias=None, endpoint=None,
                           server_binary=None, n_ctx=None, write=False)
    assert cmd_setup(args) == 0
    assert not (home / "config.toml").exists()
    assert "would be written" in capsys.readouterr().out


def test_run_refuses_unknown_profile(repo_copy, state_dir, capsys):
    args = SimpleNamespace(repo=str(repo_copy), ticket_file=str(FIXTURE_DIR / "ticket.md"),
                           profile="d9", state_dir=str(state_dir))
    assert cmd_run(args) == 1
    assert "unknown profile" in capsys.readouterr().err


def test_accept_dry_run_changes_nothing(repo_copy, state_dir):
    controller, state = _solve(repo_copy, state_dir, "ep-dry")
    repair = {rel: (repo_copy / rel).read_bytes() for rel in state.last_patch.files}
    args = SimpleNamespace(episode="ep-dry", repo=str(repo_copy), state_dir=str(state_dir),
                           dry_run=True)
    assert cmd_accept(args) == 0
    for rel, data in repair.items():
        assert (repo_copy / rel).read_bytes() == data
    types = [event["type"] for event in controller.store.read_events("ep-dry")]
    assert "accept_intent" not in types and "accepted" not in types


def test_accept_rollback_restores_applied_file(repo_copy, state_dir, monkeypatch):
    from local_coder import accept as accept_mod

    controller, state = _solve(repo_copy, state_dir, "ep-rollback")
    extra = "local-coder.toml"
    state.last_patch.files = [*state.last_patch.files, extra]
    state.last_patch.after[extra] = sha256_file(controller.store.candidate_dir("ep-rollback") / extra)
    controller.store.save_state(state)
    originals = {rel: (repo_copy / rel).read_bytes() for rel in state.last_patch.files}
    real_replace = accept_mod.os_replace
    calls = {"n": 0}

    def flaky_replace(tmp, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated replace failure")
        return real_replace(tmp, dst)

    monkeypatch.setattr(accept_mod, "os_replace", flaky_replace)
    args = SimpleNamespace(episode="ep-rollback", repo=str(repo_copy), state_dir=str(state_dir))
    assert cmd_accept(args) == 1
    for rel, data in originals.items():
        assert (repo_copy / rel).read_bytes() == data
    events = controller.store.read_events("ep-rollback")
    failed = [e for e in events if e["type"] == "accept_failed"]
    assert failed and failed[-1]["data"]["rolled_back"] == 1
    assert not list(repo_copy.rglob("*.accepttmp"))


def test_resume_refuses_profile_mismatch(repo_copy, state_dir, capsys):
    controller, _ = make_controller(repo_copy, state_dir, [], episode_id="ep-mismatch",
                                    profile_identity={"profile_name": "d1"})
    controller.start()
    args = SimpleNamespace(episode="ep-mismatch", profile="d2", state_dir=str(state_dir))
    assert cmd_resume(args) == 1
    assert "refusing to resume" in capsys.readouterr().err


def test_resume_refuses_legacy_episode_under_named_profile(repo_copy, state_dir, capsys):
    controller, _ = make_controller(repo_copy, state_dir, [], episode_id="ep-legacy")
    controller.start()
    args = SimpleNamespace(episode="ep-legacy", profile="d2", state_dir=str(state_dir))
    assert cmd_resume(args) == 1
    assert "predates named profiles" in capsys.readouterr().err


def test_doctor_probe_records_profile_and_protocol(tmp_path, monkeypatch):
    from local_coder import cli as cli_mod
    from local_coder import server as server_mod

    home = tmp_path / "machine"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    machine.write_machine_config(machine.MachineConfig(
        model_file=str(model), server_binary=sys.executable, endpoint="http://127.0.0.1:18096"))
    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.py").write_text("x = 1\n")
    assert cmd_init(SimpleNamespace(repo=str(repo), force=False)) == 0
    calls = {}

    class FakeAdapter:
        endpoint = "http://127.0.0.1:18096"

        def probe(self, save_dir=None, protocol_version="1", profile=None):
            calls.update(save_dir=str(save_dir), protocol_version=protocol_version, profile=profile)
            return {"checks": {"health": "200"}}

    monkeypatch.setattr(cli_mod, "_build_llama_adapter", lambda args, profile: FakeAdapter())
    args = SimpleNamespace(repo=str(repo), probe_model=True, profile="d2",
                           state_dir=str(tmp_path / "episodes"))
    assert cmd_doctor(args) == 0
    assert calls["protocol_version"] == "3"
    assert calls["profile"] == "d2"