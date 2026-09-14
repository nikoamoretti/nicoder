"""Machine-local config: defaults, round trip, atomicity, honest status."""
import pytest

from local_coder import machine


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    return home


def test_config_dir_honors_override(config_home):
    assert machine.config_dir() == config_home
    assert machine.config_path() == config_home / "config.toml"


def test_missing_config_yields_defaults(config_home):
    cfg = machine.load_machine_config()
    assert cfg.endpoint == machine.DEFAULT_ENDPOINT
    assert cfg.server_binary == machine.DEFAULT_SERVER_BINARY
    assert cfg.n_ctx == 8192


def test_write_load_round_trip(config_home, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    cfg = machine.MachineConfig(model_file=str(model), model_alias="tiny", n_ctx=4096)
    path = machine.write_machine_config(cfg)
    assert path == config_home / "config.toml"
    assert not path.with_suffix(".toml.tmp").exists()
    loaded = machine.load_machine_config()
    assert loaded == cfg


def test_malformed_config_is_refused(config_home):
    config_home.mkdir(parents=True)
    (config_home / "config.toml").write_text("model_file = [not a string]\n")
    with pytest.raises(ValueError):
        machine.load_machine_config()


def test_machine_status_reports_missing_and_present_model(config_home, tmp_path):
    missing = machine.MachineConfig(model_file=str(tmp_path / "nope.gguf"))
    rows = dict((name, ok) for name, ok, _ in machine.machine_status(missing))
    assert rows["model file"] is False
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x" * 16)
    present = machine.MachineConfig(model_file=str(model), server_binary="definitely-not-a-binary")
    rows = dict((name, ok) for name, ok, _ in machine.machine_status(present))
    assert rows["model file"] is True
    assert rows["llama-server binary"] is False
    assert rows["endpoint"] is True