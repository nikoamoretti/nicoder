"""Server lifecycle: ownership, health, refusals, and no foreign kills."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from local_coder import machine
from local_coder import server as server_mod
from local_coder.server import ServerError, server_start, server_status, server_stop


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    return home


@pytest.fixture
def stub_binary(tmp_path):
    stub = tmp_path / "stub-llama-server"
    stub.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
    stub.chmod(0o755)
    return stub


def test_start_refuses_without_model(config_home, monkeypatch):
    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: False)
    with pytest.raises(ServerError, match="no model configured"):
        server_start(machine.MachineConfig())


def test_start_refuses_missing_model_file(config_home, monkeypatch):
    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: False)
    with pytest.raises(ServerError, match="model file missing"):
        server_start(machine.MachineConfig(model_file="/nope/model.gguf"))


def test_start_refuses_missing_binary(config_home, tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: False)
    with pytest.raises(ServerError, match="binary not found"):
        server_start(machine.MachineConfig(model_file=str(model), server_binary="no-such-server-xyz"))


def test_start_refuses_unowned_serving_endpoint(config_home, monkeypatch):
    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: True)
    with pytest.raises(ServerError, match="already serving"):
        server_start(machine.MachineConfig())


def test_start_status_stop_lifecycle(config_home, tmp_path, stub_binary, monkeypatch):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    cfg = machine.MachineConfig(model_file=str(model), server_binary=str(stub_binary),
                                endpoint="http://127.0.0.1:18099")
    health_calls = {"n": 0}

    def fake_health(endpoint, timeout=2.0):
        health_calls["n"] += 1
        return health_calls["n"] > 1

    monkeypatch.setattr(server_mod, "_health", fake_health)
    monkeypatch.setattr(server_mod, "_props", lambda endpoint, timeout=3.0: {"model_alias": "stub-model"})
    result = server_start(cfg, wait_s=10)
    assert result["model_alias"] == "stub-model"
    record = json.loads((config_home / "server.json").read_text())
    assert record["pid"] == result["pid"]
    assert "--reasoning" in record["argv"] and "off" in record["argv"]
    status = server_status(cfg)
    assert status == {"endpoint": cfg.endpoint, "owned": True, "running": True, "healthy": True,
                      "pid": result["pid"], "model_alias": "stub-model", "note": ""}
    monkeypatch.setattr(server_mod, "_process_argv",
                        lambda pid: " ".join(str(a) for a in record["argv"]))
    stopped = server_stop()
    assert stopped == {"stopped": True, "pid": result["pid"]}
    assert not (config_home / "server.json").exists()


def test_status_reports_stale_pid(config_home, monkeypatch):
    config_home.mkdir(parents=True)
    (config_home / "server.json").write_text(json.dumps({"pid": 999999, "argv": []}))
    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: False)
    status = server_status(machine.MachineConfig())
    assert status["running"] is False and "stale" in status["note"]


def test_start_replaces_stale_owner_record(config_home, tmp_path, stub_binary, monkeypatch):
    config_home.mkdir(parents=True)
    (config_home / "server.json").write_text(json.dumps({"pid": 999999, "argv": ["gone"]}))
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    cfg = machine.MachineConfig(model_file=str(model), server_binary=str(stub_binary),
                                endpoint="http://127.0.0.1:18098")
    health_calls = {"n": 0}

    def fake_health(endpoint, timeout=2.0):
        health_calls["n"] += 1
        return health_calls["n"] > 1

    monkeypatch.setattr(server_mod, "_health", fake_health)
    monkeypatch.setattr(server_mod, "_props", lambda endpoint, timeout=3.0: {})
    result = server_start(cfg, wait_s=10)
    assert result["pid"] != 999999
    os.kill(result["pid"], 15)


def test_stop_refuses_mismatched_process(config_home, monkeypatch):
    config_home.mkdir(parents=True)
    (config_home / "server.json").write_text(json.dumps({"pid": os.getpid(), "argv": ["other", "--cmd"]}))
    monkeypatch.setattr(server_mod, "_process_argv", lambda pid: "python -m pytest tests")
    with pytest.raises(ServerError, match="refusing to signal"):
        server_stop()
    assert (config_home / "server.json").exists()


def test_stop_without_record_is_refused(config_home):
    with pytest.raises(ServerError, match="no local-coder server"):
        server_stop()


def test_port_from_endpoint():
    assert server_mod._port("http://127.0.0.1:8080") == "8080"
    assert server_mod._port("http://127.0.0.1") == "8080"


def test_cli_server_status_and_stop(config_home, monkeypatch):
    from local_coder.cli import cmd_server

    monkeypatch.setattr(server_mod, "_health", lambda endpoint, timeout=2.0: False)
    assert cmd_server(SimpleNamespace(server_command="status")) == 0
    assert cmd_server(SimpleNamespace(server_command="stop")) == 1