"""Hosted Nemotron: key lookup, streamed replies folded into one completion, no local server."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from local_coder import hosted, machine
from local_coder.app_service import AppError, select_model

SERVED = "private/nvidia/nemotron-3.5-super-120b-a12b"
SECRET = "nvapi-test-secret"


def _hosted_cfg(**overrides) -> machine.MachineConfig:
    base = {
        "model_alias": "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "endpoint": "https://integrate.api.nvidia.com",
        "provider": "nvidia",
        "api_key_name": "NVIDIA_API_KEY",
        "n_ctx": 65536,
        "max_tokens": 16384,
        "quantization": "hosted",
    }
    base.update(overrides)
    return machine.MachineConfig(**base)


def _plant(home: Path, *parts: str) -> Path:
    path = home.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"gguf")
    path.chmod(0o755)
    return path


def _sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(event)}" for event in events] + ["", "data: [DONE]"]


def _reply_lines(text: str = "Here it is.", finish: str = "stop") -> list[str]:
    return _sse(
        {"id": "c1", "model": SERVED, "choices": [{"index": 0, "delta": {"reasoning_content": "Plan it."}}]},
        {"id": "c1", "model": SERVED, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]},
        {"id": "c1", "model": SERVED, "choices": [],
         "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}},
    )


class _Response:
    def __init__(self, status_code: int = 200, lines: list[str] | None = None, body: dict | None = None):
        self.status_code = status_code
        self._lines = lines or []
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self):
        return b""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _install_client(monkeypatch, *responses) -> list[dict]:
    calls: list[dict] = []
    queue = list(responses)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append({"client": kwargs})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            calls.append({"method": method, "url": url, "json": json, "headers": headers})
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(hosted.httpx, "Client", FakeClient)
    monkeypatch.setattr(hosted.time, "sleep", lambda _seconds: None)
    return calls


def _requests(calls: list[dict]) -> list[dict]:
    return [call for call in calls if "url" in call]


def test_find_api_key_prefers_environment_then_keychain(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(hosted, "read_keychain", lambda service: asked.append(service) or "from-keychain")
    monkeypatch.setenv("NVIDIA_API_KEY", "from-env")
    assert hosted.find_api_key("NVIDIA_API_KEY") == ("from-env", "environment variable NVIDIA_API_KEY")
    assert asked == []
    monkeypatch.delenv("NVIDIA_API_KEY")
    assert hosted.find_api_key("NVIDIA_API_KEY") == ("from-keychain", "Keychain item key:NVIDIA_API_KEY")
    assert asked == ["key:NVIDIA_API_KEY"]


def test_missing_key_is_reported_by_name_only():
    value, where = hosted.find_api_key("NVIDIA_API_KEY")
    assert value == ""
    assert "key:NVIDIA_API_KEY" in where


def test_hosted_chat_streams_and_folds_one_reply(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    calls = _install_client(monkeypatch, _Response(lines=_reply_lines()))
    data = hosted.hosted_chat(_hosted_cfg(), {"model": "m", "messages": [], "stream": False}, thinking=True)
    sent = _requests(calls)[0]
    assert calls[0]["client"]["trust_env"] is False
    assert sent["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert sent["json"]["stream"] is True
    assert sent["json"]["chat_template_kwargs"] == {"enable_thinking": True}
    choice = data["choices"][0]
    assert choice["message"]["content"] == "Here it is."
    assert choice["message"]["reasoning_content"] == "Plan it."
    assert choice["finish_reason"] == "stop"
    assert data["model"] == SERVED
    assert data["usage"]["completion_tokens"] == 7
    assert data["timings"]["attempts"] == 1


def test_dropped_stream_is_retried_once(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    half = _sse({"id": "c0", "model": SERVED, "choices": [{"index": 0, "delta": {"content": "Half"}}]})[:-1]
    calls = _install_client(monkeypatch, _Response(lines=half), _Response(lines=_reply_lines("Whole.")))
    data = hosted.hosted_chat(_hosted_cfg(), {"messages": []}, thinking=False)
    assert data["choices"][0]["message"]["content"] == "Whole."
    assert data["timings"]["attempts"] == 2
    assert len(_requests(calls)) == 2


def test_two_dropped_connections_raise_after_the_second(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    dropped = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    calls = _install_client(monkeypatch, dropped, dropped)
    with pytest.raises(hosted.HostedError, match="two attempts"):
        hosted.hosted_chat(_hosted_cfg(), {"messages": []}, thinking=False)
    assert len(_requests(calls)) == 2


def test_refused_key_is_not_retried_and_never_echoed(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    body = {"status": 401, "title": "Unauthorized", "detail": "Authentication failed"}
    calls = _install_client(monkeypatch, _Response(status_code=401, body=body))
    with pytest.raises(hosted.HostedError) as info:
        hosted.hosted_chat(_hosted_cfg(), {"messages": []}, thinking=False)
    assert "HTTP 401" in str(info.value)
    assert "Authentication failed" in str(info.value)
    assert SECRET not in str(info.value)
    assert len(_requests(calls)) == 1


def test_missing_key_stops_before_any_request(monkeypatch):
    calls = _install_client(monkeypatch)
    with pytest.raises(hosted.HostedError, match="NVIDIA key was not found"):
        hosted.hosted_chat(_hosted_cfg(), {"messages": []}, thinking=False)
    assert calls == []


def test_hosted_config_round_trips_without_the_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    cfg = _hosted_cfg()
    path = machine.write_machine_config(cfg)
    assert machine.load_machine_config() == cfg
    assert SECRET not in path.read_text(encoding="utf-8")
    rows = {name: (ok, detail) for name, ok, detail in machine.machine_status(cfg)}
    assert rows["API key"] == (True, "environment variable NVIDIA_API_KEY")
    assert "model file" not in rows


def test_older_config_without_provider_loads_as_local():
    machine.config_dir().mkdir(parents=True, exist_ok=True)
    machine.config_path().write_text('model_alias = "x"\nendpoint = "http://127.0.0.1:8080"\n', encoding="utf-8")
    cfg = machine.load_machine_config()
    assert cfg.provider == "local"
    assert not machine.is_hosted(cfg)


def test_list_models_offers_nemotron_only_with_a_key(tmp_path, monkeypatch):
    from local_coder.catalog import list_models

    row = next(r for r in list_models(machine.MachineConfig(), home=tmp_path)["models"]
               if r["id"] == "nemotron35-super")
    assert row["available"] is False
    assert row["selected"] is False
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    payload = list_models(_hosted_cfg(), home=tmp_path)
    row = next(r for r in payload["models"] if r["id"] == "nemotron35-super")
    assert row["available"] is True
    assert row["selected"] is True
    assert payload["selected"] == "nemotron35-super"
    assert [r["id"] for r in payload["models"] if r["selected"]] == ["nemotron35-super"]


def test_select_nemotron_writes_hosted_config_and_frees_the_local_model(monkeypatch):
    from local_coder import app_service

    stopped: list[bool] = []
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: stopped.append(True) or {"ok": True})
    result = select_model("nemotron35-super")
    assert result["selected"] == "nemotron35-super"
    assert result["catalog"]["selected"] == "nemotron35-super"
    assert "loads" not in result["detail"]
    assert stopped == [True]
    cfg = machine.load_machine_config()
    assert cfg.provider == "nvidia"
    assert cfg.endpoint == "https://integrate.api.nvidia.com"
    assert cfg.model_alias == "nvidia/nemotron-3.5-super-vl-120b-a12b"
    assert (cfg.model_file, cfg.server_binary) == ("", "")
    assert (cfg.n_ctx, cfg.max_tokens) == (65536, 16384)
    assert cfg.api_key_name == "NVIDIA_API_KEY"
    assert SECRET not in machine.config_path().read_text(encoding="utf-8")


def test_select_nemotron_without_a_key_is_refused(monkeypatch):
    from local_coder import app_service

    monkeypatch.setattr(app_service, "stop_owned_server", lambda: pytest.fail("the local model must keep running"))
    with pytest.raises(AppError) as info:
        select_model("nemotron35-super")
    assert info.value.code == "missing_key"


def test_switching_back_to_a_local_model_restores_the_loopback_endpoint(tmp_path, monkeypatch):
    from local_coder import app_service
    from local_coder import catalog as catalog_mod

    gguf = _plant(tmp_path, "Models", "Bonsai-demo", "models", "bonsai2-gguf", "27B",
                  "Ternary-Bonsai-2-27B-PQ2_0.gguf")
    _plant(tmp_path, "Models", "Bonsai-demo", "bin", "mac", "llama-server")
    monkeypatch.setattr(catalog_mod, "_home", lambda: tmp_path)
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True, "stopped": False})
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    select_model("nemotron35-super")
    select_model("bonsai2-27b")
    cfg = machine.load_machine_config()
    assert cfg.provider == "local"
    assert cfg.endpoint == machine.DEFAULT_ENDPOINT
    assert cfg.api_key_name == ""
    assert Path(cfg.model_file) == gguf


def test_helper_startup_keeps_the_hosted_context_window(monkeypatch):
    from local_coder import app_service

    machine.write_machine_config(_hosted_cfg())
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True, "stopped": False})
    assert app_service.ensure_machine_config()["config"]["n_ctx"] == 65536
    assert machine.load_machine_config().n_ctx == 65536


def test_hosted_model_needs_no_local_server(monkeypatch):
    from local_coder import app_service, server
    from local_coder.general.coordinator import ensure_model_server

    machine.write_machine_config(_hosted_cfg())
    monkeypatch.setattr(server, "_health", lambda *a, **k: pytest.fail("no local health probe for a hosted model"))
    with pytest.raises(server.ServerError, match="no local server"):
        server.server_start(machine.load_machine_config())
    status = server.server_status(machine.load_machine_config())
    assert status["hosted"] is True
    assert status["running"] is False
    monkeypatch.setattr(app_service, "server_start", lambda *a, **k: pytest.fail("no llama-server for a hosted model"))
    monkeypatch.setattr(server, "server_start", lambda *a, **k: pytest.fail("no llama-server for a hosted model"))
    result = app_service.ensure_server()
    assert (result["started"], result["hosted"]) == (False, True)
    assert ensure_model_server()["hosted"] is True


def test_readiness_for_a_hosted_model_checks_the_key_not_a_server(monkeypatch):
    from local_coder import app_service

    machine.write_machine_config(_hosted_cfg())
    monkeypatch.setattr(app_service, "isolation_status", lambda: {"ok": False, "image": "x", "detail": "docker down"})
    payload = app_service.readiness()
    rows = {row["name"]: row for row in payload["rows"]}
    assert rows["API key"]["ok"] is False
    assert payload["ok"] is False
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    payload = app_service.readiness()
    rows = {row["name"]: row for row in payload["rows"]}
    assert payload["ok"] is True
    assert payload["blocking"] == []
    assert rows["API key"]["detail"] == "environment variable NVIDIA_API_KEY"
    assert rows["model server"]["ok"] is True
    assert payload["selected_model"] == "nemotron35-super"
    assert SECRET not in json.dumps(payload)


def test_local_complete_uses_the_hosted_model_with_thinking(monkeypatch):
    from local_coder.general import direct_write

    monkeypatch.setattr(direct_write, "load_machine_config", _hosted_cfg)
    monkeypatch.setattr(direct_write, "server_start", lambda *a, **k: pytest.fail("no local server"))
    monkeypatch.setattr(
        "local_coder.engines.context_budget.LlamaCppTokenizer.count",
        lambda self, text: pytest.fail("no llama.cpp tokenizer for a hosted model"),
    )
    seen: dict = {}

    def fake_chat(cfg, payload, *, thinking):
        seen.update(payload=payload, thinking=thinking)
        return {
            "id": "c1",
            "model": SERVED,
            "choices": [{"message": {"content": "```write src/a.ts\nexport {}\n```"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 20, "total_tokens": 29},
            "timings": {"first_token_s": 0.5, "stream_s": 1.0, "attempts": 1},
        }

    monkeypatch.setattr(direct_write, "hosted_chat", fake_chat)
    result = direct_write.local_complete("Write src/a.ts")
    assert seen["thinking"] is True
    assert seen["payload"]["max_tokens"] == 16384
    assert result.text.startswith("```write src/a.ts")
    assert result.truncated is False
    assert result.request_identity["model"] == SERVED
    assert result.timing["account_counter"] == "estimate"
    assert result.timing["attempts"] == 1


def test_local_complete_reports_a_hosted_refusal(monkeypatch):
    from local_coder.general import direct_write

    def refused(*args, **kwargs):
        raise hosted.HostedError("NVIDIA refused the request (HTTP 401: Authentication failed).")

    monkeypatch.setattr(direct_write, "load_machine_config", _hosted_cfg)
    monkeypatch.setattr(direct_write, "hosted_chat", refused)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        direct_write.local_complete("Write src/a.ts")


def test_hosted_prompt_budget_uses_the_larger_window(monkeypatch):
    from local_coder.general import direct_write

    monkeypatch.setattr(direct_write, "load_machine_config", _hosted_cfg)
    hosted_budget = direct_write.prompt_budget_chars()
    monkeypatch.setattr(direct_write, "load_machine_config",
                        lambda: machine.MachineConfig(n_ctx=8192, max_tokens=3072))
    assert hosted_budget > 4 * direct_write.prompt_budget_chars()


def test_chat_uses_the_hosted_model_without_thinking(tmp_path, monkeypatch):
    from local_coder import app_service

    machine.write_machine_config(_hosted_cfg())
    seen: dict = {}

    def fake_chat(cfg, payload, *, thinking):
        seen.update(payload=payload, thinking=thinking)
        return {"choices": [{"message": {"content": "It is a task board."}}]}

    monkeypatch.setattr(app_service, "hosted_chat", fake_chat)
    monkeypatch.setattr(app_service, "server_start", lambda *a, **k: pytest.fail("no local server"))
    root = tmp_path / "board"
    root.mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    reply = app_service.chat_request(root, "what is this project?")
    assert reply["summary"] == "It is a task board."
    assert seen["thinking"] is False
    assert seen["payload"]["max_tokens"] == 512


def test_legacy_engine_refuses_a_hosted_model(tmp_path):
    from local_coder.app_service import run_request

    machine.write_machine_config(_hosted_cfg())
    with pytest.raises(AppError) as info:
        run_request(tmp_path, "fix the failing test", profile_name="d2")
    assert info.value.code == "unsupported"
