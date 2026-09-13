"""Prompt 3 adapter: constrained requests, usage, truncation, probe.

A local mock speaks the llama.cpp server's documented HTTP shapes so request
construction, constraint verification, and usage accounting are tested
exactly. Live-server tests stay separate and are NOT RUN without a server.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_coder.inference.base import AdapterError
from local_coder.inference.llama_cpp import LlamaCppAdapter, check_configured
from local_coder.inference.tokenizer import EstimateCounter

ACTION = '{"action":"done","status":"blocked","reason_code":"probe","summary":"probe"}'


class MockState:
    """Canned server behavior plus captured requests."""

    def __init__(self):
        self.requests = []
        self.chat_body = {"content": ACTION, "finish_reason": "stop",
                          "usage": {"prompt_tokens": 120, "completion_tokens": 25}}
        self.chat_status = 200


class Handler(BaseHTTPRequestHandler):
    """Minimal documented-shape mock of the llama.cpp server API."""

    state = None

    def _send(self, code, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/props":
            self._send(200, {"total_slots": 4})
        else:
            self._send(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        Handler.state.requests.append((self.path, data))
        if self.path == "/v1/chat/completions":
            if Handler.state.chat_status != 200:
                self._send(Handler.state.chat_status, {"error": "boom"})
                return
            self._send(200, {"choices": [{"message": {"role": "assistant",
                                                      "content": Handler.state.chat_body["content"]},
                                        "finish_reason": Handler.state.chat_body["finish_reason"]}],
                             "usage": Handler.state.chat_body["usage"]})
        elif self.path == "/tokenize":
            text = data.get("content", "")
            self._send(200, {"tokens": list(range(len(text.split())))})
        else:
            self._send(404, {"error": "nope"})

    def log_message(self, *args):
        pass


@pytest.fixture
def mock_server():
    """Running mock llama.cpp server plus its observable state."""
    state = MockState()
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def _adapter(mock_server, **kwargs):
    url, _ = mock_server
    return LlamaCppAdapter(url, model="mock-model", **kwargs)


def test_generate_sends_json_constraint_and_records_usage(mock_server):
    url, state = mock_server
    adapter = _adapter(mock_server)
    raw = adapter.generate("briefing", 0)
    assert raw == ACTION
    paths = [path for path, _ in state.requests]
    assert "/v1/chat/completions" in paths
    payload = dict(state.requests[-1][1])
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    assert adapter.last_usage == {"finish_reason": "stop", "prompt_tokens": 120,
                                  "completion_tokens": 25}


def test_finish_length_recorded_for_truncation_path(mock_server):
    _, state = mock_server
    state.chat_body = {"content": '{"action":"fin', "finish_reason": "length",
                       "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    adapter = _adapter(mock_server)
    raw = adapter.generate("briefing", 0)
    assert raw == '{"action":"fin'
    assert adapter.last_usage["finish_reason"] == "length"


def test_truncated_output_becomes_protocol_error(repo_copy, state_dir):
    from local_coder.inference.fake import ScriptedFakeAdapter


    class TruncatingFake(ScriptedFakeAdapter):
        def generate(self, briefing, step):
            self.calls += 1
            self.last_usage = {"finish_reason": "length", "prompt_tokens": 10,
                               "completion_tokens": 5}
            return '{"action":"fin'

    import sys

    from local_coder.config import load_config
    from local_coder.controller import Controller
    from local_coder.execution.trusted_local import TrustedLocalBackend
    from local_coder.persistence import EpisodeStore

    config = load_config(repo_copy)
    config.commands = {s: [sys.executable, *a[1:]] for s, a in config.commands.items()}
    store = EpisodeStore(state_dir)
    controller = Controller(store, config, TruncatingFake(["x"]), TrustedLocalBackend(),
                            repo_copy, "ticket")
    controller.start()
    assert controller.step() is True
    assert controller.last_observation["code"] == "truncated_output"


def test_redirect_is_refused():
    class RedirectHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = LlamaCppAdapter(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(AdapterError, match="redirect"):
            adapter.generate("briefing", 0)
    finally:
        server.shutdown()


def test_non_200_is_adapter_error(mock_server):
    _, state = mock_server
    state.chat_status = 500
    with pytest.raises(AdapterError, match="HTTP 500"):
        _adapter(mock_server).generate("briefing", 0)


def test_tokenize_counts_come_from_server(mock_server):
    adapter = _adapter(mock_server)
    counter = adapter.token_counter()
    assert counter.exact is True
    assert counter.count("one two three four") == 4


def test_estimate_counter_is_labeled_estimate():
    counter = EstimateCounter()
    assert counter.exact is False
    assert counter.count("abcd") == 1
    assert counter.count("x" * 400) == 100


def test_probe_verifies_constraint_tokenize_and_saves_record(mock_server, tmp_path):
    url, _ = mock_server
    adapter = _adapter(mock_server)
    record = adapter.probe(save_dir=tmp_path)
    assert record["checks"]["json_constraint"] == "honored"
    assert record["checks"]["action_parse"] == "ok"
    assert record["checks"]["tokenize"].startswith("ok")
    saved = list((tmp_path / "_probes").glob("probe-*.json"))
    assert len(saved) == 1


def test_probe_fails_when_constraint_violated(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "just some prose, not json", "finish_reason": "stop",
                       "usage": {}}
    with pytest.raises(AdapterError, match="JSON constraint violated"):
        _adapter(mock_server).probe()


def test_probe_fails_when_action_unparseable(mock_server):
    _, state = mock_server
    state.chat_body = {"content": '{"hello": "world"}', "finish_reason": "stop", "usage": {}}
    with pytest.raises(AdapterError, match="did not yield a valid action"):
        _adapter(mock_server).probe()


def test_provenance_leaves_unknowns_unknown(mock_server):
    provenance = _adapter(mock_server).provenance()
    assert provenance["adapter"] == "llama.cpp"
    assert provenance["model_sha256"] == "unknown"
    assert provenance["quantization"] == "unknown"
    assert provenance["n_ctx_source"] == "unknown"


def test_provenance_hashes_configured_model_file(mock_server, tmp_path):
    weights = tmp_path / "model.gguf"
    weights.write_bytes(b"fake-weights")
    adapter = _adapter(mock_server, model_file=str(weights), quantization="Q4_K_M", n_ctx=8192)
    provenance = adapter.provenance()
    assert len(provenance["model_sha256"]) == 64
    assert provenance["quantization"] == "Q4_K_M"
    assert provenance["n_ctx"] == "8192"


def test_unconfigured_url_is_not_run():
    from local_coder.inference.base import AdapterUnavailable

    with pytest.raises(AdapterUnavailable):
        check_configured(None)
