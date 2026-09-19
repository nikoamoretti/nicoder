"""Model catalog: discover installed files, select without adopting a foreign server."""
from pathlib import Path

import pytest

from local_coder import machine
from local_coder.app_rpc import RpcServer
from local_coder.app_service import AppError, select_model
from local_coder.catalog import known_entries, list_models


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv(machine.CONFIG_DIR_ENV, str(home))
    return home


def _plant(home: Path, *parts: str, data: bytes = b"gguf") -> Path:
    path = home.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def test_known_entries_include_bonsai_and_qwen(tmp_path):
    ids = [row.id for row in known_entries(tmp_path)]
    assert ids == ["bonsai2-27b", "qwen35-9b", "qwen35-4b"]
    bonsai = next(row for row in known_entries(tmp_path) if row.id == "bonsai2-27b")
    assert bonsai.n_ctx == 8192
    assert bonsai.gen_timeout == 600
    assert bonsai.max_tokens == 3072
    assert bonsai.server_binary.endswith("Bonsai-demo/bin/mac/llama-server")
    assert bonsai.model_file.endswith("Ternary-Bonsai-2-27B-PQ2_0.gguf")


def test_list_models_marks_available_and_selected(config_home, tmp_path):
    gguf = _plant(tmp_path, "Models", "Bonsai-demo", "models", "bonsai2-gguf", "27B",
                  "Ternary-Bonsai-2-27B-PQ2_0.gguf")
    binary = _plant(tmp_path, "Models", "Bonsai-demo", "bin", "mac", "llama-server")
    cfg = machine.MachineConfig(
        model_file=str(gguf),
        model_alias="Ternary-Bonsai-2-27B-PQ2_0",
        server_binary=str(binary),
        n_ctx=16384,
        quantization="PQ2_0",
    )
    payload = list_models(cfg, home=tmp_path)
    bonsai = next(row for row in payload["models"] if row["id"] == "bonsai2-27b")
    assert bonsai["available"] is True
    assert bonsai["selected"] is True
    assert payload["selected"] == "bonsai2-27b"
    qwen = next(row for row in payload["models"] if row["id"] == "qwen35-9b")
    assert qwen["available"] is False
    assert qwen["selected"] is False


def test_select_model_writes_bonsai_paths(config_home, tmp_path, monkeypatch):
    from local_coder import app_service
    from local_coder import catalog as catalog_mod

    gguf = _plant(tmp_path, "Models", "Bonsai-demo", "models", "bonsai2-gguf", "27B",
                  "Ternary-Bonsai-2-27B-PQ2_0.gguf")
    binary = _plant(tmp_path, "Models", "Bonsai-demo", "bin", "mac", "llama-server")
    monkeypatch.setattr(catalog_mod, "_home", lambda: tmp_path)
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True, "stopped": False})
    result = select_model("bonsai2-27b")
    assert result["selected"] == "bonsai2-27b"
    assert "Open or create a project" in result["detail"]
    loaded = machine.load_machine_config()
    assert Path(loaded.model_file) == gguf
    assert loaded.model_alias == "Ternary-Bonsai-2-27B-PQ2_0"
    assert Path(loaded.server_binary) == binary
    assert loaded.n_ctx == 8192
    assert loaded.quantization == "PQ2_0"
    assert loaded.gen_timeout == 600
    assert loaded.max_tokens == 3072


def test_apply_speed_defaults_raises_bonsai_generation_budget(config_home, tmp_path, monkeypatch):
    from local_coder import app_service
    from local_coder import catalog as catalog_mod

    gguf = _plant(
        tmp_path,
        "Models",
        "Bonsai-demo",
        "models",
        "bonsai2-gguf",
        "27B",
        "Ternary-Bonsai-2-27B-PQ2_0.gguf",
    )
    binary = _plant(tmp_path, "Models", "Bonsai-demo", "bin", "mac", "llama-server")
    monkeypatch.setattr(catalog_mod, "_home", lambda: tmp_path)
    machine.write_machine_config(
        machine.MachineConfig(
            model_file=str(gguf),
            model_alias="Ternary-Bonsai-2-27B-PQ2_0",
            server_binary=str(binary),
            n_ctx=8192,
            quantization="PQ2_0",
            max_tokens=2048,
            gen_timeout=300,
        )
    )
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True, "stopped": False})
    cfg = app_service.apply_speed_defaults()
    assert cfg.gen_timeout == 600
    assert cfg.max_tokens == 3072
    loaded = machine.load_machine_config()
    assert loaded.gen_timeout == 600
    assert loaded.max_tokens == 3072


def test_select_model_refuses_missing_id(config_home):
    with pytest.raises(AppError, match="Unknown model"):
        select_model("nope")


def test_rpc_list_and_select_models(config_home, tmp_path, monkeypatch):
    from local_coder import app_service
    from local_coder import catalog as catalog_mod

    _plant(tmp_path, ".cache", "local-coder", "models", "Qwen_Qwen3.5-9B-Q4_K_M.gguf")
    monkeypatch.setattr(catalog_mod, "_home", lambda: tmp_path)
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True, "stopped": False})
    monkeypatch.setattr(app_service, "shutil", __import__("shutil"))
    server = RpcServer()
    listed = server.dispatch({"id": "1", "method": "list_models", "params": {}})
    assert listed["ok"] is True
    assert listed["result"]["models"]
    # 9B file exists but llama-server may or may not be on PATH in CI; selecting
    # still requires the binary. Plant a fake llama-server name only if needed.
    planted = _plant(tmp_path, "bin", "llama-server")
    monkeypatch.setenv("PATH", str(planted.parent) + ":" + __import__("os").environ.get("PATH", ""))
    chosen = server.dispatch({"id": "2", "method": "select_model", "params": {"id": "qwen35-9b"}})
    assert chosen["ok"] is True
    assert chosen["result"]["selected"] == "qwen35-9b"
    assert machine.load_machine_config().model_alias == "Qwen3.5-9B-Q4_K_M"


def test_readiness_does_not_block_when_docker_is_down(config_home, monkeypatch):
    from local_coder import app_service

    monkeypatch.setattr(app_service, "machine_status", lambda cfg: [
        ("model file", True, "ok"),
        ("llama-server binary", True, "ok"),
        ("endpoint", True, "http://127.0.0.1:8080"),
    ])
    monkeypatch.setattr(app_service, "server_status", lambda cfg: {
        "endpoint": "http://127.0.0.1:8080",
        "owned": False,
        "running": False,
        "healthy": False,
        "pid": None,
        "model_alias": None,
        "note": "",
    })
    monkeypatch.setattr(app_service, "isolation_status", lambda: {
        "ok": False,
        "image": "local-coder-worker:1",
        "detail": "docker daemon unreachable",
    })
    monkeypatch.setattr(app_service, "list_models", lambda cfg: {
        "models": [],
        "selected": "bonsai2-27b",
        "current_alias": "Ternary-Bonsai-2-27B-PQ2_0",
    })
    payload = app_service.readiness()
    assert payload["ok"] is True
    assert payload["blocking"] == []
    isolated = next(row for row in payload["rows"] if row["name"] == "isolated tests")
    assert isolated["ok"] is False


def test_load_prefs_hides_folders_that_no_longer_exist(config_home, tmp_path):
    from local_coder.app_service import load_prefs, save_prefs

    gone = tmp_path / "vanished"
    stay = tmp_path / "keep"
    gone.mkdir()
    stay.mkdir()
    save_prefs({
        "recent_projects": [
            {"path": str(gone), "name": "vanished"},
            {"path": str(stay), "name": "keep"},
        ],
        "profile": "d2",
        "engine": "general",
    })
    gone.rmdir()
    prefs = load_prefs()
    assert [row["name"] for row in prefs["recent_projects"]] == ["keep"]


def test_rpc_ensure_server_starts_owned_model(config_home, monkeypatch):
    from local_coder import app_rpc

    monkeypatch.setattr(app_rpc, "ensure_server", lambda wait_s=180: {
        "ok": True, "started": True, "status": {"owned": True, "healthy": True},
    })
    server = RpcServer()
    reply = server.dispatch({"id": "3", "method": "ensure_server", "params": {}})
    assert reply["ok"] is True
    assert reply["result"]["started"] is True
