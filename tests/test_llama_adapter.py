"""Prompt 3/4 adapter: constrained streaming requests, usage, transport.

A local mock speaks the llama.cpp server's documented HTTP shapes (SSE
chat stream, tokenize, props, health) so request construction, constraint
verification, byte caps, cancellation, and usage accounting are tested
exactly. Live-server tests stay separate and are NOT RUN without a server.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_coder.inference.base import AdapterError
from local_coder.inference.grammar import action_grammar, constraint_id
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
        self.channel = "content"
        self.drip_delay = 0.0
        self.close_early = False
        # When set, these reasoning deltas stream alongside empty content,
        # emulating a thinking model whose final channel stays empty.
        self.reasoning_stream: str | None = None
        # Transport scenarios: raw stream bytes override the canned chunks;
        # hold_after_done keeps the connection open after [DONE]; send_done
        # controls the terminal marker; extra_metadata pads every event.
        self.raw_stream: bytes | None = None
        self.hold_after_done_s = 0.0
        self.send_done = True
        self.extra_metadata: dict | None = None


def _sse_chunks(content, finish, usage, channel):
    yield {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None, "index": 0}]}
    half = max(1, len(content) // 2)
    for piece in (content[:half], content[half:]):
        if piece:
            yield {"choices": [{"delta": {channel: piece}, "finish_reason": None, "index": 0}]}
    yield {"choices": [{"delta": {}, "finish_reason": finish, "index": 0}], "usage": usage}


class Handler(BaseHTTPRequestHandler):
    """Minimal documented-shape mock of the llama.cpp server API.

    HTTP/1.0 close-delimited responses: returning early simulates a server
    crash mid-stream without hanging the client on keep-alive.
    """

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
            state = Handler.state
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if state.raw_stream is not None:
                try:
                    self.wfile.write(state.raw_stream)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if state.hold_after_done_s:
                    time.sleep(state.hold_after_done_s)
                return
            chunks = list(_sse_chunks(state.chat_body["content"],
                                      state.chat_body["finish_reason"],
                                      state.chat_body["usage"], state.channel))
            if state.reasoning_stream is not None:
                extra = list(_sse_chunks(state.reasoning_stream, None, {}, "reasoning_content"))
                chunks[1:1] = [c for c in extra if c["choices"][0]["delta"]]
            if state.extra_metadata:
                for chunk in chunks:
                    chunk.update(state.extra_metadata)
            for index, chunk in enumerate(chunks):
                if state.drip_delay:
                    time.sleep(state.drip_delay)
                if state.close_early and index == 1:
                    return
                line = f"data: {json.dumps(chunk)}\n\n".encode()
                try:
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            if state.send_done:
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            if state.hold_after_done_s:
                time.sleep(state.hold_after_done_s)
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


def test_generate_sends_schema_constraint_and_records_usage(mock_server):
    url, state = mock_server
    adapter = _adapter(mock_server)
    raw = adapter.generate("briefing", 0)
    assert raw == ACTION
    paths = [path for path, _ in state.requests]
    assert "/v1/chat/completions" in paths
    payload = dict(state.requests[-1][1])
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is True
    assert "grammar" in payload and '\\"action\\"' in payload["grammar"]
    assert adapter.last_request["payload"]["grammar"] == payload["grammar"]
    assert adapter.last_usage["finish_reason"] == "stop"
    assert adapter.last_usage["prompt_tokens"] == 120
    assert adapter.last_usage["completion_tokens"] == 25
    assert adapter.last_usage["channel"] == "content"


def test_reasoning_channel_fallback_is_recorded(mock_server):
    _, state = mock_server
    state.channel = "reasoning_content"
    adapter = _adapter(mock_server)
    assert adapter.generate("briefing", 0) == ACTION
    assert adapter.last_usage["channel"] == "reasoning_content"


def test_grammar_text_covers_action_schema():
    grammar = action_grammar()
    for literal in ('\\"find\\"', '\\"patch\\"', '\\"check\\"', '\\"done\\"',
                    '\\"apply\\"', '\\"revert_last\\"', '\\"focused\\"',
                    '\\"regression\\"', '\\"solved\\"', '\\"blocked\\"',
                    '\\"query\\"', '\\"symbols\\"', '\\"diff\\"',
                    '\\"expected_effect\\"', '\\"scope\\"', '\\"summary\\"',
                    '\\"reason_code\\"', '\\"operation\\"', '\\"status\\"'):
        assert literal in grammar, literal
    assert constraint_id() == "gbnf-action-v1"
    # Structural guard: the "action" key is consumed once, in root; the
    # alternatives are value fragments, never nested action objects.
    assert grammar.count('\\"action\\"') == 1
    for line in grammar.splitlines():
        if line.startswith(("find-action", "patch-action", "check-action", "done-action")):
            assert '"{"' not in line.split("::=", 1)[1]


def test_json_only_constraint_sends_no_grammar(mock_server):
    adapter = _adapter(mock_server, constraint="json_object")
    adapter.generate("briefing", 0)
    _, state = mock_server
    payload = dict(state.requests[-1][1])
    assert "grammar" not in payload
    assert adapter.last_request["payload"]["response_format"] == {"type": "json_object"}


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
    assert record["constraint"] == "gbnf-action-v1"
    assert record["checks"]["constraint"] == "json_parsed"
    assert record["checks"]["action_parse"] == "ok"
    assert record["checks"]["tokenize"].startswith("ok")
    saved = list((tmp_path / "_probes").glob("probe-*.json"))
    assert len(saved) == 1


def test_probe_fails_when_constraint_violated(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "just some prose, not json", "finish_reason": "stop",
                       "usage": {}}
    with pytest.raises(AdapterError, match="non-JSON"):
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


def test_slow_drip_hits_overall_deadline(mock_server):
    _, state = mock_server
    state.drip_delay = 0.6
    adapter = _adapter(mock_server, timeout_s=2, read_timeout_s=30)
    with pytest.raises(AdapterError, match="deadline"):
        adapter.generate("briefing", 0)
    assert adapter.last_usage is None


def test_oversized_body_aborted_while_receiving(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "x" * 200000, "finish_reason": "stop",
                       "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    adapter = _adapter(mock_server, response_max_bytes=1024)
    with pytest.raises(AdapterError, match="transport cap"):
        adapter.generate("briefing", 0)
    assert adapter.last_usage is None
    diagnostics = adapter.last_failure
    assert diagnostics["limit"] == "transport_max_bytes"
    assert diagnostics["limit_value"] == 1024
    assert diagnostics["transport_policy"] == "sse-transport-v2"
    assert diagnostics["total_received_bytes"] > 1024
    # The cap can fire before a complete event line is buffered; the event
    # counter reports exactly what was processed (0 or more).
    assert diagnostics["event_count"] >= 0
    assert diagnostics["finish_reason"] in ("unknown", "stop")


def test_interrupted_stream_is_an_error(mock_server):
    _, state = mock_server
    state.close_early = True
    adapter = _adapter(mock_server)
    with pytest.raises(AdapterError, match="completion evidence"):
        adapter.generate("briefing", 0)
    assert adapter.last_usage is None
    assert adapter.last_failure["limit"] == "missing_completion"
    assert adapter.last_failure["terminal_marker"] is False


def test_legit_multievent_completion_with_substantial_metadata(mock_server):
    _, state = mock_server
    state.extra_metadata = {
        "id": "chatcmpl-" + "a" * 64,
        "object": "chat.completion.chunk",
        "created": 1789275237,
        "model": "mock-model",
        "system_fingerprint": "b" * 32,
        "timings": {"prompt_n": 4, "prompt_ms": 134.9},
    }
    adapter = _adapter(mock_server)
    raw = adapter.generate("briefing", 0)
    assert raw == ACTION
    usage = adapter.last_usage
    assert usage["transport_policy"] == "sse-transport-v2"
    assert usage["streamed_bytes"] > 0
    assert usage["decoded_bytes"] == len(ACTION.encode("utf-8"))
    assert usage["finish_reason"] == "stop"
    assert adapter.last_failure is None


def test_excessive_structural_whitespace_is_bounded(mock_server):
    _, state = mock_server
    # A runaway structural run inside the final event, like the observed
    # degenerate generation: spaces only, no closing action.
    state.raw_stream = b"data: " + b" " * 70000 + b"\n\n"
    adapter = _adapter(mock_server, event_max_bytes=8192, response_max_bytes=131072)
    with pytest.raises(AdapterError, match="event cap"):
        adapter.generate("briefing", 0)
    assert adapter.last_failure["limit"] == "max_event_bytes"
    assert adapter.last_failure["limit_value"] == 8192
    # The whitespace run is structural: nothing was decoded into content, so
    # the honest sample is empty and the decoded counter stays at zero.
    assert adapter.last_failure["decoded_content_bytes"] == 0
    assert adapter.last_failure["output_prefix"] == ""


def test_long_unterminated_event_is_bounded_while_receiving(mock_server):
    _, state = mock_server
    state.raw_stream = b"data: {\"choices\":[{\"delta\":{\"content\":\"" + b"x" * 50000
    adapter = _adapter(mock_server, event_max_bytes=4096, response_max_bytes=131072)
    with pytest.raises(AdapterError, match="event cap"):
        adapter.generate("briefing", 0)
    assert adapter.last_failure["limit"] == "max_event_bytes"
    assert adapter.last_failure["total_received_bytes"] >= 4096


def test_truncation_after_content_without_finish_is_incomplete(mock_server):
    _, state = mock_server
    part1 = {"choices": [{"delta": {"content": '{"action":"done",'}, "finish_reason": None, "index": 0}]}
    part2 = {"choices": [{"delta": {"content": '"status":"blocked"}'}, "finish_reason": None, "index": 0}]}
    state.raw_stream = (
        b"data: " + json.dumps(part1).encode() + b"\n\n"
        + b"data: " + json.dumps(part2).encode() + b"\n\n"
    )
    adapter = _adapter(mock_server)
    with pytest.raises(AdapterError, match="completion evidence"):
        adapter.generate("briefing", 0)
    diagnostics = adapter.last_failure
    assert diagnostics["finish_reason"] == "unknown"
    assert diagnostics["terminal_marker"] is False
    assert diagnostics["decoded_content_bytes"] > 0
    assert diagnostics["event_count"] == 2
    assert "action" in diagnostics["output_tail"] or "action" in diagnostics["output_prefix"]


def test_terminal_marker_with_open_connection_returns_promptly(mock_server):
    _, state = mock_server
    final = {"choices": [{"delta": {"content": ACTION}, "finish_reason": "stop", "index": 0}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    state.raw_stream = (
        b"data: " + json.dumps(final).encode() + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    state.hold_after_done_s = 30.0  # connection stays open after completion
    adapter = _adapter(mock_server, timeout_s=60)
    started = time.monotonic()
    assert adapter.generate("briefing", 0) == ACTION
    assert time.monotonic() - started < 10.0, "terminal marker must not wait for close"
    assert adapter.last_usage["finish_reason"] == "stop"


def test_decoded_action_size_is_separate_from_transport_size(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "y" * 9000, "finish_reason": "stop",
                       "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    adapter = _adapter(mock_server, decoded_max_bytes=4096, response_max_bytes=131072)
    with pytest.raises(AdapterError, match="decoded action"):
        adapter.generate("briefing", 0)
    diagnostics = adapter.last_failure
    assert diagnostics["limit"] == "decoded_max_bytes"
    assert diagnostics["limit_value"] == 4096
    assert diagnostics["decoded_content_bytes"] > 4096
    assert diagnostics["total_received_bytes"] < 131072, \
        "decoded cap must fire earlier than the transport cap for this stream"


def test_brace_inside_string_does_not_end_action(mock_server):
    _, state = mock_server
    content = '{"action":"done","status":"blocked","reason_code":"probe","summary":"braces { } inside"}'
    first = {"choices": [{"delta": {"content": content[:40]}, "finish_reason": None, "index": 0}]}
    second = {"choices": [{"delta": {"content": content[40:]}, "finish_reason": "stop", "index": 0}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    state.raw_stream = (
        b"data: " + json.dumps(first).encode() + b"\n\n"
        + b"data: " + json.dumps(second).encode() + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    adapter = _adapter(mock_server)
    raw = adapter.generate("briefing", 0)
    assert raw == content
    assert adapter.last_usage["finish_reason"] == "stop"


def test_cancel_closes_request_and_discards_late_bytes(mock_server):
    import threading as _threading

    _, state = mock_server
    state.drip_delay = 0.4
    adapter = _adapter(mock_server, timeout_s=60, read_timeout_s=2)
    outcome = {}

    def run():
        try:
            outcome["raw"] = adapter.generate("briefing", 0)
        except AdapterError as exc:
            outcome["error"] = str(exc)

    worker = _threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.5)
    adapter.cancel()
    worker.join(timeout=20)
    assert "error" in outcome and "cancelled" in outcome["error"]
    assert adapter.last_usage is None
    assert "raw" not in outcome


def test_cancelled_dispatch_consumes_slot_without_state_change(repo_copy, state_dir):
    import threading as _threading
    import time as _time

    from local_coder.inference.fake import ScriptedFakeAdapter

    from .conftest import make_controller

    class CancellableFake(ScriptedFakeAdapter):
        def __init__(self):
            super().__init__(["x"])
            self._cancel = _threading.Event()

        def cancel(self):
            self._cancel.set()

        def generate(self, briefing, step):
            self.calls += 1
            for _ in range(100):
                if self._cancel.is_set():
                    from local_coder.inference.base import AdapterError as _AE

                    raise _AE("cancelled by supervisor")
                _time.sleep(0.05)
            return '{"action":"done","status":"blocked","reason_code":"x","summary":"y"}'

    controller, adapter = make_controller(repo_copy, state_dir, [])
    controller.adapter = CancellableFake()
    controller.start()
    used_before = controller.state.generations_used
    outcome = {}

    def run():
        try:
            outcome["cont"] = controller.step()
        except Exception as exc:  # noqa: BLE001 - must not escape
            outcome["escaped"] = repr(exc)

    worker = _threading.Thread(target=run, daemon=True)
    worker.start()
    _time.sleep(0.3)
    controller.adapter.cancel()
    worker.join(timeout=30)
    assert "escaped" not in outcome
    assert outcome.get("cont") is True
    assert controller.state.generations_used == used_before + 1
    assert controller.last_observation["status"] == "adapter_error"
    assert controller.state.fingerprints == []


def test_final_only_accepts_final_channel(mock_server):
    adapter = _adapter(mock_server, channel_policy="final-only")
    assert adapter.generate("briefing", 0) == ACTION
    assert adapter.last_usage["channel"] == "content"


def test_final_only_rejects_reasoning_only_output(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "", "finish_reason": "stop",
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    state.reasoning_stream = "thinking without ever answering"
    adapter = _adapter(mock_server, channel_policy="final-only")
    with pytest.raises(AdapterError, match="reasoning-only output is a compatibility failure"):
        adapter.generate("briefing", 0)
    assert adapter.last_usage is None


def test_legacy_fallback_still_reads_reasoning_channel(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "", "finish_reason": "stop",
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    state.reasoning_stream = ACTION
    adapter = _adapter(mock_server, channel_policy="legacy")
    assert adapter.generate("briefing", 0) == ACTION
    assert adapter.last_usage["channel"] == "reasoning_content"
    assert adapter.last_usage["reasoning_present"] is True


def test_invalid_channel_policy_rejected(mock_server):
    url, _ = mock_server
    with pytest.raises(AdapterError, match="unknown channel policy"):
        LlamaCppAdapter(url, channel_policy="everything")


def test_invalid_thinking_mode_rejected(mock_server):
    url, _ = mock_server
    with pytest.raises(AdapterError, match="unknown thinking mode"):
        LlamaCppAdapter(url, thinking="maximum")


def test_thinking_off_probe_requires_content_without_reasoning(mock_server, tmp_path):
    _, state = mock_server
    adapter = _adapter(mock_server, thinking="off")
    record = adapter.probe(save_dir=tmp_path)
    assert record["thinking"] == "off"
    assert record["channel"] == "content"
    assert adapter.provenance()["thinking"] == "off"


def test_thinking_off_probe_fails_on_reasoning_output(mock_server):
    _, state = mock_server
    state.chat_body = {"content": "", "finish_reason": "stop",
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    state.reasoning_stream = ACTION
    adapter = _adapter(mock_server, thinking="off")
    with pytest.raises(AdapterError, match="reasoning output|final-answer content"):
        adapter.probe()


def test_v2_grammar_sent_for_v2_constraint(mock_server):
    from local_coder.inference.grammar import constraint_id_v2

    adapter = _adapter(mock_server, constraint=constraint_id_v2())
    adapter.generate("briefing", 0)
    _, state = mock_server
    payload = dict(state.requests[-1][1])
    assert '\\"replace\\"' in payload["grammar"]
    assert '\\"apply\\"' not in payload["grammar"]
    assert '\\"edits\\"' in payload["grammar"]
    assert adapter.provenance()["constraint"] == constraint_id_v2()


def test_v3_grammar_sent_for_v3_constraint(mock_server):
    from local_coder.inference.grammar import constraint_id_v3

    adapter = _adapter(mock_server, constraint=constraint_id_v3())
    adapter.generate("briefing", 0)
    _, state = mock_server
    payload = dict(state.requests[-1][1])
    assert '\\"replace_span\\"' in payload["grammar"]
    assert '\\"span_id\\"' in payload["grammar"]
    assert '\\"old\\"' not in payload["grammar"]
    assert adapter.provenance()["constraint"] == constraint_id_v3()
