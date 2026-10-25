"""OrcaRouter: the model list, keeping the key in the Keychain, browser sign-in, requests and cost."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from local_coder import hosted, machine, orcarouter
from local_coder.app_service import AppError

KEY = "sk-orca-TestKey1234567890abcdef"
SONNET = {
    "id": "anthropic/claude-sonnet-5.5", "name": "Anthropic: Claude Sonnet 5.5",
    "endpoint_capabilities": ["chat_completions", "anthropic_messages"],
    "supported_endpoint_types": ["openai", "anthropic"], "context_length": 1000000, "max_completion_tokens": 128000,
    "architecture": {"input_modalities": ["text", "image", "file"], "output_modalities": ["text"]},
    "pricing": {"prompt_per_million": "2.000000", "completion_per_million": "10.000000"},
    "supported_parameters": ["tools", "reasoning", "max_tokens"],
    "description": "Claude Sonnet 5.5 is the current Sonnet. It does more things.",
}
GLM_FREE = {"id": "z-ai/glm-5.3-flash-free", "name": "Z.ai: GLM 5.3 Flash (Free)",
            "endpoint_capabilities": ["chat_completions"], "pricing": {"request": "0.000000"}}
KIMI = {"id": "kimi/kimi-k2.7-code", "name": "MoonshotAI: Kimi K2.7 Code",
        "endpoint_capabilities": ["chat_completions"], "context_length": 262144,
        "pricing": {"prompt_per_million": "0.95", "completion_per_million": "4.0"}, "supported_parameters": ["tools"]}
PRO = {"id": "openai/gpt-5.4-pro", "name": "OpenAI: GPT-5.4 Pro", "endpoint_capabilities": ["chat_completions"],
       "pricing": {"prompt_per_million": "60", "completion_per_million": "270"}, "supported_parameters": ["tools"]}
NOT_CHAT = [
    {"id": "openai/gpt-image-1", "supported_endpoint_types": ["image-generation"]},
    {"id": "google/gemini-3.1-flash-image-preview", "endpoint_capabilities": ["chat_completions"],
     "architecture": {"output_modalities": ["text", "image"]}},
    {"id": "openai/text-embedding-3-small", "supported_endpoint_types": ["embeddings"]},
    {"id": "orcarouter/fusion", "supported_endpoint_types": ["openai"]},
    {"id": "openai/o1-mini", "endpoint_capabilities": ["chat_completions"], "supported_parameters": ["max_tokens"]},
    {"id": "openai/gpt-5-search-api", "endpoint_capabilities": ["chat_completions"], "supported_parameters": ["tools"]},
]
BENCH = [
    {"model": "claude-sonnet-5.5", "intelligence": 56, "tokens_per_sec": 89.1},
    {"model": "glm-5.3-flash", "intelligence": 41.8, "tokens_per_sec": 120.4},
    {"model": "kimi-k2.7-code", "intelligence": 44.0},
    {"model": "gpt-5.4-pro", "intelligence": 66.0},
]


class FakeKeychain:
    """Stands in for /usr/bin/security: one service, any number of stacked items."""

    def __init__(self, monkeypatch) -> None:
        self.values: list[str] = []
        self.commands: list[str] = []
        monkeypatch.setattr(orcarouter, "_security", self.security)
        monkeypatch.setattr(orcarouter, "_item_exists", lambda: bool(self.values))
        monkeypatch.setattr(hosted, "read_keychain",
                            lambda service: self.values[0] if service == orcarouter.KEY_SERVICE and self.values else "")

    def security(self, command: str) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        words = command.split()
        if words[0] == "delete-generic-password" and self.values:
            self.values.pop(0)
        if words[0] == "add-generic-password":
            self.values.append(words[words.index("-w") + 1])
        return subprocess.CompletedProcess(["security", "-i"], 0, "", "")


@pytest.fixture
def keychain(monkeypatch, tmp_path) -> FakeKeychain:
    monkeypatch.setattr(orcarouter.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return FakeKeychain(monkeypatch)


def _cache(models: list[dict], *, age_s: float = 0) -> None:
    path = orcarouter.catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": time.time() - age_s, "models": models}), encoding="utf-8")


def test_only_chat_models_that_call_tools_are_offered():
    models = orcarouter.normalize([SONNET, GLM_FREE, *NOT_CHAT, {"id": "orcarouter/free"}], BENCH)
    assert [row["id"] for row in models] == ["anthropic/claude-sonnet-5.5", "z-ai/glm-5.3-flash-free"]
    sonnet, glm = models
    assert (sonnet["name"], sonnet["provider"], sonnet["context"]) == ("Claude Sonnet 5.5", "Anthropic", 1000000)
    assert (sonnet["price_in"], sonnet["price_out"], sonnet["free"]) == (2.0, 10.0, False)
    assert sonnet["reasoning"] and sonnet["vision"] and (sonnet["intelligence"], sonnet["speed"]) == (56.0, 89)
    assert sonnet["description"] == "Claude Sonnet 5.5 is the current Sonnet."
    assert (glm["name"], glm["provider"], glm["free"], glm["intelligence"]) == ("GLM 5.3 Flash (Free)", "Z.ai", True,
                                                                               41.8)
    assert orcarouter.price_label(sonnet) == "$2 in · $10 out" and orcarouter.price_label(glm) == "Free"


def test_suggestions_are_the_smartest_affordable_models_plus_a_free_one():
    models = orcarouter.normalize([SONNET, GLM_FREE, KIMI, PRO], BENCH)
    assert orcarouter.suggested(models) == ["anthropic/claude-sonnet-5.5", "kimi/kimi-k2.7-code",
                                            "z-ai/glm-5.3-flash-free"]
    _cache(models)
    assert orcarouter.menu_models()[:2] == ["anthropic/claude-sonnet-5.5", "kimi/kimi-k2.7-code"]
    orcarouter.remember_model("kimi/kimi-k2.7-code")
    orcarouter.remember_model("z-ai/glm-5.3-flash-free")
    assert orcarouter.menu_models("anthropic/claude-sonnet-5.5") == [
        "anthropic/claude-sonnet-5.5", "z-ai/glm-5.3-flash-free", "kimi/kimi-k2.7-code"]


def test_the_list_is_downloaded_joined_and_cached(monkeypatch):
    seen: list[str] = []

    class Response:
        def __init__(self, body: dict) -> None:
            self.status_code = 200
            self.body = body

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self.body

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            seen.append(url)
            if url.endswith("/v1/models"):
                return Response({"data": [SONNET, *NOT_CHAT]})
            return Response({"success": True, "data": {"items": BENCH}})

    monkeypatch.setattr(orcarouter.httpx, "Client", Client)
    catalog = orcarouter.load_catalog()
    assert [row["id"] for row in catalog["models"]] == ["anthropic/claude-sonnet-5.5"]
    assert seen == ["https://api.orcarouter.ai/v1/models", "https://api.orcarouter.ai/api/public/benchmark-board"]
    assert orcarouter.find_model("anthropic/claude-sonnet-5.5")["intelligence"] == 56.0
    orcarouter.load_catalog()
    assert len(seen) == 2, "a fresh cache is reused without a download"


def test_a_pasted_key_is_checked_then_kept_in_the_keychain(keychain, monkeypatch, tmp_path):
    (tmp_path / "home" / ".config" / "key").mkdir(parents=True)
    monkeypatch.setattr(orcarouter, "verify_key", lambda value: True)
    status = orcarouter.save_pasted_key(f"  {KEY}\n")
    assert status["connected"] is True and status["detail"] == "OrcaRouter is connected."
    assert status["where"] == "Keychain item key:ORCAROUTER_API_KEY"
    assert keychain.values == [KEY]
    assert KEY not in json.dumps(status)
    listing = (tmp_path / "home" / ".config" / "key" / "keychain.txt").read_text()
    assert listing.split() == ["key:ORCAROUTER_API_KEY"]


def test_refused_and_malformed_keys_are_not_kept(keychain, monkeypatch):
    monkeypatch.setattr(orcarouter, "verify_key", lambda value: False)
    with pytest.raises(orcarouter.OrcaError, match="did not accept") as refused:
        orcarouter.save_pasted_key(KEY)
    assert KEY not in str(refused.value)
    monkeypatch.setattr(orcarouter, "verify_key", lambda value: pytest.fail("no network for a malformed key"))
    with pytest.raises(orcarouter.OrcaError, match="start with sk-orca-"):
        orcarouter.save_pasted_key("sk-proj-not an orca key")
    assert keychain.values == [] and keychain.commands == []


def test_a_new_key_replaces_every_older_item(keychain):
    keychain.values = ["sk-orca-old-one-1234567", "sk-orca-old-two-1234567"]
    orcarouter.store_key(KEY)
    assert keychain.values == [KEY]
    assert orcarouter.remove_key()["connected"] is False and keychain.values == []


def _hit(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 — loopback test server
        assert b"Nicoder" in response.read()


def test_sign_in_in_the_browser_keeps_the_new_key(keychain, monkeypatch):
    exchanged: dict = {}

    class Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"key": KEY, "user_id": "42", "scope": "api"}

    def post(url, json=None, timeout=None, trust_env=None):
        exchanged.update(url=url, **json)
        return Reply()

    monkeypatch.setattr(orcarouter.httpx, "post", post)
    opened: dict = {}

    def browser(url: str) -> None:
        query = {name: values[0] for name, values in parse_qs(urlparse(url).query).items()}
        opened.update(query)
        callback = query["callback_url"]
        assert callback.startswith("http://127.0.0.1:") and callback.endswith("/callback")
        _hit(f"{callback}?code=one-time-code&state={query['state']}")

    said: list[str] = []
    status = orcarouter.sign_in(open_url=browser, on_status=said.append)
    assert status["connected"] is True and keychain.values == [KEY]
    assert opened["code_challenge_method"] == "S256" and opened["app_name"] == "Nicoder"
    assert opened["scope"] == "api"
    digest = hashlib.sha256(exchanged["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == opened["code_challenge"]
    assert exchanged["url"] == "https://www.orcarouter.ai/api/v1/auth/keys" and exchanged["code"] == "one-time-code"
    assert said[0].startswith("Approve Nicoder")
    assert KEY not in json.dumps(status)


@pytest.mark.parametrize(("query", "message"), [
    ("code=abc&state=forged", "did not match"),
    ("error=access_denied&state={state}", "declined"),
])
def test_sign_in_refuses_a_forged_or_declined_reply(keychain, monkeypatch, query, message):
    monkeypatch.setattr(orcarouter.httpx, "post", lambda *a, **k: pytest.fail("no exchange"))

    def browser(url: str) -> None:
        params = {name: values[0] for name, values in parse_qs(urlparse(url).query).items()}
        _hit(f"{params['callback_url']}?{query.format(state=params['state'])}")

    with pytest.raises(orcarouter.OrcaError, match=message):
        orcarouter.sign_in(open_url=browser)
    assert keychain.values == []


def test_sign_in_stops_when_cancelled(keychain):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(orcarouter.OrcaError, match="cancelled"):
        orcarouter.sign_in(open_url=lambda url: None, cancel=cancel)
    assert time.monotonic() - started < 3


def _orca_cfg(**overrides) -> machine.MachineConfig:
    base = {"model_alias": "kimi/kimi-k2.7-code", "endpoint": "https://api.orcarouter.ai", "provider": "orcarouter",
            "api_key_name": "ORCAROUTER_API_KEY", "n_ctx": 131072, "max_tokens": 16384, "quantization": "hosted"}
    base.update(overrides)
    return machine.MachineConfig(**base)


class _Stream:
    def __init__(self, status_code: int = 200, lines: list[str] | None = None, body: dict | None = None) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._body = body
        self.headers: dict = {}

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


def _client(monkeypatch, *responses) -> list[dict]:
    calls: list[dict] = []
    queue = list(responses)

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return queue.pop(0)

    monkeypatch.setattr(hosted.httpx, "Client", Client)
    monkeypatch.setattr(hosted.time, "sleep", lambda seconds: None)
    return calls


def _sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(event)}" for event in events] + ["data: [DONE]"]


def test_requests_ask_for_cost_and_group_a_run(monkeypatch):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    calls = _client(monkeypatch, _Stream(lines=_sse(
        {"id": "r1", "model": "kimi/kimi-k2.7-code",
         "choices": [{"index": 0, "delta": {"reasoning_content": "Think."}}]},
        {"id": "r1", "choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": "stop"}]},
        {"id": "r1", "choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40, "cost_usd": 0.0123}},
    )))
    data = hosted.hosted_chat(_orca_cfg(), {"model": "kimi/kimi-k2.7-code", "messages": []}, thinking=True,
                              session="local-coder-abc")
    sent = calls[0]
    assert sent["url"] == "https://api.orcarouter.ai/v1/chat/completions"
    assert sent["headers"]["X-OrcaRouter-Include-Cost"] == "true"
    assert sent["headers"]["X-OrcaRouter-Session-Id"] == "local-coder-abc"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    assert "chat_template_kwargs" not in sent["json"] and sent["json"]["stream_options"] == {"include_usage": True}
    assert data["usage"]["cost_usd"] == 0.0123
    assert data["choices"][0]["message"]["content"] == "Done."


@pytest.mark.parametrize(("response", "message"), [
    (_Stream(402, body={"error": {"code": "insufficient_user_quota", "message": "quota"}}), "balance is empty"),
    (_Stream(401, body={"error": {"message": "invalid token"}}), "did not accept the API key"),
    (_Stream(429, body={"error": {"code": "free_rate_limited", "message": "denied",
                                  "metadata": {"reason": "err_free_access_denied", "retryable": False}}}),
     "refused the request"),
    (_Stream(503, body={"error": {"code": "model_not_found", "message": "no"}}), "does not offer this model"),
])
def test_refusals_are_explained_and_not_retried(monkeypatch, response, message):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    calls = _client(monkeypatch, response)
    with pytest.raises(hosted.HostedError, match=message) as refused:
        hosted.hosted_chat(_orca_cfg(), {"messages": []}, thinking=True)
    assert len(calls) == 1
    assert KEY not in str(refused.value)


def test_an_error_inside_the_stream_is_retried(monkeypatch):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    broken = _Stream(lines=["data: " + json.dumps({"error": {"message": "upstream reset", "type": "upstream_error"}})])
    fine = _Stream(lines=_sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}))
    calls = _client(monkeypatch, broken, fine)
    data = hosted.hosted_chat(_orca_cfg(), {"messages": []}, thinking=True)
    assert data["choices"][0]["message"]["content"] == "ok" and len(calls) == 2


def test_orcarouter_models_keep_their_own_sampling_and_one_session_per_run(monkeypatch):
    from local_coder.agent import runner

    sent: list[tuple[dict, dict]] = []
    monkeypatch.setattr(runner, "hosted_chat", lambda cfg, body, **kwargs: sent.append((body, kwargs)) or {})
    chat = runner.hosted_tool_chat(_orca_cfg())
    chat([{"role": "user", "content": "hi"}], [])
    chat([{"role": "user", "content": "again"}], [])
    assert "temperature" not in sent[0][0] and sent[0][0]["max_tokens"] == 16384
    assert sent[0][1]["session"] == sent[1][1]["session"] and sent[0][1]["session"].startswith("local-coder-")
    assert runner.hosted_tool_chat(_orca_cfg())  # a new run gets a new chat function
    nvidia = machine.MachineConfig(provider="nvidia", model_alias="m", api_key_name="NVIDIA_API_KEY")
    runner.hosted_tool_chat(nvidia)([], [])
    assert sent[-1][0]["temperature"] == 0.3


def test_a_question_reports_what_it_cost(tmp_path):
    from local_coder.agent.runner import run_ask

    replies = [
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "list", "arguments": json.dumps({"path": "~"})}}]},
            "finish_reason": "tool_calls"}], "usage": {"cost_usd": 0.01}},
        {"choices": [{"message": {"role": "assistant", "content": "Two folders."}, "finish_reason": "stop"}],
         "usage": {"cost_usd": 0.025}},
    ]
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    asked = run_ask("What is in ~?", project=None, chat=lambda messages, tools: replies.pop(0),
                    context_chars=50_000, home=home)
    assert asked.answer == "Two folders." and asked.cost_usd == 0.035


def test_orcarouter_models_appear_in_the_picker_and_need_a_key(monkeypatch):
    from local_coder import app_service
    from local_coder.catalog import list_models

    _cache(orcarouter.normalize([SONNET, GLM_FREE, KIMI], BENCH))
    rows = [row for row in list_models(machine.MachineConfig())["models"] if row["provider"] == "orcarouter"]
    assert [row["id"] for row in rows][:2] == ["orca:anthropic/claude-sonnet-5.5", "orca:kimi/kimi-k2.7-code"]
    assert {row["group"] for row in rows} == {"OrcaRouter"} and not any(row["available"] for row in rows)
    assert rows[0]["subtitle"] == "$2 in · $10 out" and rows[0]["intelligence"] == 56.0
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True})
    with pytest.raises(AppError, match="Connect OrcaRouter to use Kimi K2.7 Code") as missing:
        app_service.select_model("orca:kimi/kimi-k2.7-code")
    assert missing.value.code == "missing_key"
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    result = app_service.select_model("orca:z-ai/glm-5.3-flash-free")
    cfg = machine.load_machine_config()
    assert (cfg.provider, cfg.endpoint, cfg.model_alias, cfg.api_key_name) == (
        "orcarouter", "https://api.orcarouter.ai", "z-ai/glm-5.3-flash-free", "ORCAROUTER_API_KEY")
    assert (cfg.n_ctx, cfg.max_tokens, cfg.temperature) == (131072, 16384, 0.0)
    assert result["catalog"]["selected"] == "orca:z-ai/glm-5.3-flash-free"
    assert orcarouter.recent_models() == ["z-ai/glm-5.3-flash-free"]
    assert "OrcaRouter" in machine.machine_status(cfg)[0][2]


def test_any_listed_model_can_be_picked_from_the_full_list(monkeypatch):
    from local_coder import app_service

    _cache(orcarouter.normalize([SONNET, KIMI, PRO], BENCH))
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True})
    listing = app_service.orca_models()
    assert listing["connected"] is True and listing["selected"] == ""
    assert [row["id"] for row in listing["models"]] == ["anthropic/claude-sonnet-5.5", "kimi/kimi-k2.7-code",
                                                       "openai/gpt-5.4-pro"]
    app_service.select_model("orca:openai/gpt-5.4-pro")
    assert app_service.orca_models()["selected"] == "openai/gpt-5.4-pro"


def test_sign_in_runs_beside_other_calls_and_can_be_cancelled(monkeypatch):
    from local_coder import app_rpc

    def waiting_sign_in(listener, cancel):
        listener({"type": "orca_sign_in", "phrase": "Approve Nicoder in the browser window that just opened."})
        assert cancel.wait(5)
        raise AppError("Sign-in was cancelled.", code="orcarouter")

    monkeypatch.setattr(app_rpc, "orca_sign_in", waiting_sign_in)
    out = io.StringIO()
    server = app_rpc.RpcServer(out=out)
    lines = [
        {"id": "sign", "method": "orca_sign_in", "params": {}},
        {"id": "status", "method": "orca_status", "params": {}},
        {"id": "stop", "method": "orca_sign_in_cancel", "params": {}},
    ]
    server.serve(io.StringIO("\n".join(json.dumps(line) for line in lines) + "\n"))
    assert server._sign_in_thread is not None
    server._sign_in_thread.join(10)
    replies = [json.loads(row) for row in out.getvalue().splitlines() if row.strip()]
    answers = {row["id"]: row for row in replies if row.get("id")}
    assert answers["status"]["result"]["connected"] is False
    assert answers["sign"]["error"] == {"code": "orcarouter", "detail": "Sign-in was cancelled."}
    order = [row.get("id") for row in replies if row.get("id")]
    assert order.index("status") < order.index("sign")
    assert any(row.get("event") == "progress" for row in replies)


def test_the_key_never_reaches_an_rpc_reply(keychain, monkeypatch):
    from local_coder import app_rpc

    monkeypatch.setattr(orcarouter, "verify_key", lambda value: True)
    out = io.StringIO()
    request = {"id": "k", "method": "orca_save_key", "params": {"key": KEY}}
    app_rpc.RpcServer(out=out).serve(io.StringIO(json.dumps(request) + "\n"))
    assert KEY not in out.getvalue() and json.loads(out.getvalue().splitlines()[-1])["result"]["connected"] is True
    monkeypatch.setattr(orcarouter, "verify_key", lambda value: (_ for _ in ()).throw(RuntimeError(value)))
    out = io.StringIO()
    app_rpc.RpcServer(out=out).serve(io.StringIO(json.dumps(request) + "\n"))
    assert KEY not in out.getvalue()
    assert json.loads(out.getvalue().splitlines()[-1])["error"]["detail"] == "The key could not be saved."


def test_status_never_shows_the_key(monkeypatch):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    status = orcarouter.key_status()
    assert status["connected"] is True and status["where"] == "environment variable ORCAROUTER_API_KEY"
    assert KEY not in json.dumps(status)
    assert Path(orcarouter.catalog_path()).name == "orcarouter-models.json"


QWEN = {"id": "qwen/qwen3.8-27b", "name": "Qwen3.8 27B", "endpoint_capabilities": ["chat_completions"],
        "supported_parameters": ["reasoning", "include_reasoning", "tools"],
        "pricing": {"prompt_per_million": "0.33", "completion_per_million": "2.4"}}
QWEN_API = {"id": "qwen/qwen3.6-flash", "name": "Qwen: Qwen3.6 Flash", "endpoint_capabilities": ["chat_completions"],
            "supported_parameters": ["enable_thinking", "reasoning", "tools"]}


def test_qwen_models_can_turn_reasoning_off_and_others_cannot():
    rows = {row["id"]: row for row in orcarouter.normalize([SONNET, QWEN, QWEN_API, KIMI], BENCH)}
    assert rows["qwen/qwen3.8-27b"]["reasoning_switch"] and rows["qwen/qwen3.6-flash"]["reasoning_switch"]
    assert not rows["anthropic/claude-sonnet-5.5"]["reasoning_switch"]
    assert not rows["kimi/kimi-k2.7-code"]["reasoning_switch"]


@pytest.mark.parametrize(("model", "thinking", "expected"), [
    ("qwen/qwen3.8-27b", False, {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}),
    ("qwen/qwen3.8-27b", True, {}),
])
def test_reasoning_off_reaches_qwen_through_both_switches(monkeypatch, model, thinking, expected):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    calls = _client(monkeypatch, _Stream(lines=_sse({"choices": [{"index": 0, "delta": {"content": "ok"},
                                                                  "finish_reason": "stop"}]})))
    hosted.hosted_chat(_orca_cfg(model_alias=model, reasoning_switch=True), {"messages": []}, thinking=thinking)
    sent = calls[0]["json"]
    assert {key: sent[key] for key in ("enable_thinking", "chat_template_kwargs") if key in sent} == expected


def test_models_without_a_switch_never_get_one(monkeypatch):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    calls = _client(monkeypatch, _Stream(lines=_sse({"choices": [{"index": 0, "delta": {"content": "ok"},
                                                                  "finish_reason": "stop"}]})))
    hosted.hosted_chat(_orca_cfg(model_alias="anthropic/claude-sonnet-5.5"), {"messages": []}, thinking=False)
    assert "enable_thinking" not in calls[0]["json"] and "chat_template_kwargs" not in calls[0]["json"]


def test_reasoning_is_remembered_per_model_and_reaches_the_agent(monkeypatch):
    from local_coder import app_service
    from local_coder.agent import runner

    _cache(orcarouter.normalize([SONNET, QWEN], BENCH))
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    monkeypatch.setattr(app_service, "stop_owned_server", lambda: {"ok": True})
    app_service.select_model("orca:qwen/qwen3.8-27b")
    cfg = machine.load_machine_config()
    assert (cfg.reasoning, cfg.reasoning_switch) == ("on", True)
    assert app_service.set_reasoning("off") == {"ok": True, "reasoning": "off", "model": "orca:qwen/qwen3.8-27b"}
    sent: list[dict] = []
    monkeypatch.setattr(runner, "hosted_chat", lambda cfg, body, **kwargs: sent.append(kwargs) or {})
    runner.hosted_tool_chat(machine.load_machine_config())([], [])
    assert sent[-1]["thinking"] is False
    app_service.select_model("orca:anthropic/claude-sonnet-5.5")
    cfg = machine.load_machine_config()
    assert (cfg.reasoning, cfg.reasoning_switch) == ("on", False)
    with pytest.raises(AppError, match="no reasoning switch"):
        app_service.set_reasoning("off")
    app_service.select_model("orca:qwen/qwen3.8-27b")
    assert machine.load_machine_config().reasoning == "off", "the choice comes back with the model"
    assert app_service.readiness()["reasoning"] == "off"


def test_a_config_saved_before_the_switch_existed_can_still_turn_reasoning_off(monkeypatch):
    from local_coder import app_service

    _cache(orcarouter.normalize([QWEN], BENCH))
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    machine.write_machine_config(_orca_cfg(model_alias="qwen/qwen3.8-27b"))  # reasoning_switch left at False
    assert app_service.set_reasoning("off")["reasoning"] == "off"
    cfg = machine.load_machine_config()
    assert (cfg.reasoning, cfg.reasoning_switch) == ("off", True)


def test_stop_cuts_a_streaming_reply_short(monkeypatch):
    monkeypatch.setenv("ORCAROUTER_API_KEY", KEY)
    cancel = threading.Event()

    class Endless(_Stream):
        def iter_lines(self):
            for n in range(1000):
                if n == 3:
                    cancel.set()
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "word "}}]})

    calls = _client(monkeypatch, Endless())
    with pytest.raises(hosted.HostedError, match="Stopped"):
        hosted.hosted_chat(_orca_cfg(), {"messages": []}, thinking=True, cancel=cancel)
    assert len(calls) == 1, "a stop is not retried"


def test_a_stopped_model_call_ends_the_run_as_stopped(tmp_path):
    from local_coder.agent.access import AccessPolicy
    from local_coder.agent.loop import AgentSession
    from local_coder.agent.tools import ToolBox

    cancel = threading.Event()

    def chat(messages, tools):
        cancel.set()
        raise hosted.HostedError("Stopped.")

    home = tmp_path / "home"
    home.mkdir()
    session = AgentSession(chat=chat, toolbox=ToolBox(AccessPolicy(home=home), cwd=home, run_tmp=tmp_path / "t"),
                           system="s", cancel=cancel)
    run = session.ask("hello")
    assert (run.status, run.answer) == ("stopped", "Stopped.")


def test_the_agent_hands_stop_to_the_model_call(monkeypatch):
    from local_coder.agent import runner

    seen: dict = {}
    monkeypatch.setattr(runner, "hosted_chat", lambda cfg, body, **kwargs: seen.update(kwargs) or {})
    cancel = threading.Event()
    runner.hosted_tool_chat(_orca_cfg(), cancel=cancel)([], [])
    assert seen["cancel"] is cancel
