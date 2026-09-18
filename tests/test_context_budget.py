"""Context bounding and dispatch checks. No live model generations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_coder.engines.context_budget import (
    ConservativeEstimator,
    ContextBudgetError,
    ContextPolicy,
    LlamaCppTokenizer,
    account,
    flatten_request,
    prepare_chat_request,
    reject_oversized_write,
)
from local_coder.engines.model_identity import ModelIdentityError, resolve_model_id, stale_mapping
from local_coder.engines.observations import ClipPolicy, ObservationStore, clip_text
from local_coder.machine import MachineConfig


def _tool_message(text: str, tool: str = "bash", call_id: str = "call_1") -> dict:
    return {"role": "tool", "name": tool, "tool_call_id": call_id, "content": text}


def _assistant_call(call_id: str = "call_1", name: str = "bash") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


def test_estimator_scales_with_input():
    counter = ConservativeEstimator()
    small = counter.count("a" * 100)
    large = counter.count("a" * 10_000)
    assert large > small
    assert large > 1000


def test_bound_379kb_result_and_report(tmp_path):
    store = ObservationStore(tmp_path / "obs", ClipPolicy(excerpt_chars=4000))
    payload = ("ls -la\n" + ("node_modules/pkg/file.js\n" * 16_000) + "ERROR: boom at end\n")
    assert len(payload.encode("utf-8")) >= 370_000
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [
            {"role": "system", "content": "Build the board."},
            {"role": "user", "content": "Implement the task board."},
            _assistant_call(),
            _tool_message(payload),
        ],
    }
    prepared, accounting = prepare_chat_request(
        body,
        store=store,
        counter=ConservativeEstimator(),
        policy=ContextPolicy(n_ctx=32768),
        expected_model="Qwen3.5-9B-Q4_K_M",
    )
    visible = prepared["messages"][-1]["content"]
    assert "ERROR: boom at end" in visible
    assert "observation_id:" in visible
    assert "omitted" in visible.lower() or "omitted_bytes" in visible
    assert len(visible) < 12_000
    assert accounting.allowed is True
    assert accounting.original_result_chars >= 370_000
    assert accounting.provider_visible_chars < accounting.original_result_chars
    assert accounting.stored_path and Path(accounting.stored_path).exists()
    assert Path(accounting.stored_path).stat().st_size >= 370_000
    assert accounting.input_tokens + accounting.output_reserve + accounting.safety_margin <= accounting.n_ctx
    report = {
        "original_result_size": accounting.original_result_chars,
        "provider_visible_observation_size": len(visible),
        "preserved_full_output": accounting.stored_path,
        "complete_assembled_input_tokens": accounting.input_tokens,
        "output_reserve": accounting.output_reserve,
        "remaining_headroom": accounting.remaining_headroom,
        "dispatch_allowed": accounting.allowed,
        "counter": accounting.counter_label,
    }
    (tmp_path / "diagnostic.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report["dispatch_allowed"] is True


def test_oversized_assistant_message_is_clipped(tmp_path):
    """The 9B overflow stored the dump on an assistant message, not only role=tool."""
    store = ObservationStore(tmp_path / "obs")
    dump = "ASSISTANT-TOOL-DUMP\n" + ("/usr/lib/node_modules/pkg/index.js\n" * 12_000)
    assert len(dump.encode("utf-8")) >= 370_000
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [
            {"role": "user", "content": "Implement the task board."},
            {"role": "assistant", "content": dump},
        ],
    }
    prepared, accounting = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    visible = prepared["messages"][-1]["content"]
    assert len(visible) < 12_000
    assert "observation_id:" in visible
    assert accounting.allowed is True
    assert accounting.original_result_chars >= 370_000


def test_oversized_tool_description_is_clipped(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "read",
                "description": "DESC " + ("x" * 20_000),
            },
        }],
    }
    prepared, accounting = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    desc = prepared["tools"][0]["function"]["description"]
    assert prepared["tools"][0]["function"]["name"] == "read"
    assert len(desc) < 8_000
    assert accounting.allowed is True


def test_large_single_line_result(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    line = "x" * 200_000
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [_tool_message(line, tool="read")],
    }
    prepared, _ = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    visible = prepared["messages"][0]["content"]
    assert len(visible) < 12_000
    assert "observation_id:" in visible


def test_several_results_collectively_exceed(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    messages = [{"role": "user", "content": "keep this requirement"}]
    for i in range(8):
        messages.append(_assistant_call(call_id=f"c{i}"))
        messages.append(_tool_message(f"chunk-{i}-" * 800, call_id=f"c{i}"))
    body = {"model": "Qwen3.5-9B-Q4_K_M", "messages": messages}
    policy = ContextPolicy(n_ctx=4000, output_reserve=200, safety_margin=50, template_overhead=10)
    prepared, accounting = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=policy, expected_model="Qwen3.5-9B-Q4_K_M",
    )
    assert accounting.allowed is True
    assert "keep this requirement" in flatten_request(prepared)
    ids = [m.get("tool_call_id") for m in prepared["messages"] if isinstance(m, dict)]
    assert "c0" in ids and "c7" in ids


def test_error_near_end_of_long_log():
    text = ("ok line\n" * 5000) + "FATAL: compile failed in TaskBoard.tsx:12\n"
    excerpt, omitted = clip_text(text, ClipPolicy())
    assert omitted is True
    assert "FATAL: compile failed" in excerpt


def test_bounded_retrieval_of_omitted_portion(tmp_path):
    store = ObservationStore(tmp_path / "obs", ClipPolicy(excerpt_chars=200, head_chars=80, tail_chars=80))
    text = "HEADDATA" + ("m" * 5000) + "TAILDATA"
    first = store.put(text, tool="read")
    assert first.omitted is True
    assert first.next_offset is not None
    second = store.read_slice(first.observation_id, offset=first.next_offset)
    assert second.offset == first.next_offset
    assert len(second.excerpt) <= 200
    assert text not in first.excerpt
    combined = first.excerpt + second.excerpt
    assert len(combined) < len(text)


def test_unicode_and_structured_output_integrity(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    text = 'café 日本語 {"ok": true, "path": "src/App.tsx"}\n' + ("α" * 8000)
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [_tool_message(text, tool="read", call_id="call_u")],
    }
    prepared, _ = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    visible = prepared["messages"][0]["content"]
    assert "café" in visible
    assert "日本語" in visible
    assert "src/App.tsx" in visible
    assert prepared["messages"][0]["tool_call_id"] == "call_u"


def test_tool_call_pairing_survives_compaction(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    messages = [
        {"role": "user", "content": "user requirement stays"},
        _assistant_call("pair-1", "read"),
        _tool_message("Z" * 50_000, tool="read", call_id="pair-1"),
    ]
    policy = ContextPolicy(n_ctx=800, output_reserve=50, safety_margin=20, template_overhead=10)
    prepared, accounting = prepare_chat_request(
        {"model": "Qwen3.5-9B-Q4_K_M", "messages": messages},
        store=store, counter=ConservativeEstimator(),
        policy=policy, expected_model="Qwen3.5-9B-Q4_K_M",
    )
    assert accounting.allowed is True
    assistant = next(m for m in prepared["messages"] if m.get("role") == "assistant")
    tool = next(m for m in prepared["messages"] if m.get("role") == "tool")
    assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"] == "pair-1"


def test_stale_4b_to_9b_mapping_is_detected():
    assert stale_mapping("llama.cpp/Qwen3.5-4B-Q4_K_M", "Qwen3.5-9B-Q4_K_M") is True
    assert stale_mapping("llama.cpp/Qwen3.5-9B-Q4_K_M", "Qwen3.5-9B-Q4_K_M") is False
    assert stale_mapping({"models": {"Qwen3.5-4B-Q4_K_M": {}}}, "Qwen3.5-9B-Q4_K_M") is True


def test_resolve_model_id_refuses_mismatch():
    cfg = MachineConfig(model_alias="Qwen3.5-9B-Q4_K_M")
    with pytest.raises(ModelIdentityError):
        resolve_model_id("Qwen3.5-4B-Q4_K_M", cfg=cfg)
    assert resolve_model_id(None, cfg=cfg) == "Qwen3.5-9B-Q4_K_M"


def test_irreducible_request_does_not_dispatch(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    huge = "KEEP-ALL-OF-THIS-USER-SPEC " * 20_000
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [{"role": "user", "content": huge}],
    }
    policy = ContextPolicy(n_ctx=2000, output_reserve=200, safety_margin=50)
    with pytest.raises(ContextBudgetError) as exc:
        prepare_chat_request(
            body, store=store, counter=ConservativeEstimator(),
            policy=policy, expected_model="Qwen3.5-9B-Q4_K_M",
        )
    assert exc.value.accounting.allowed is False
    assert "No generation was dispatched" in exc.value.detail


def test_resume_does_not_reinsert_historical_oversize(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    historical = "HISTORICAL-OVERSIZE " * 30_000
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [
            {"role": "user", "content": "continue"},
            _assistant_call("old"),
            _tool_message(historical, call_id="old"),
        ],
    }
    first, _ = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    second, _ = prepare_chat_request(
        {"model": "Qwen3.5-9B-Q4_K_M", "messages": list(first["messages"]) + [
            _assistant_call("new"),
            _tool_message(historical, call_id="new"),
        ]},
        store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    blob = flatten_request(second)
    assert historical not in blob
    assert blob.count("HISTORICAL-OVERSIZE") < 1_000
    assert "observation_id:" in blob


def test_compaction_is_local_and_does_not_call_cloud(tmp_path, monkeypatch):
    store = ObservationStore(tmp_path / "obs")
    calls: list[str] = []

    def blocked(*_a, **_k):
        calls.append("http")
        raise AssertionError("cloud or network must not be used")

    monkeypatch.setattr("httpx.Client", blocked)
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [
            {"role": "user", "content": "req"},
            _assistant_call("c1"),
            _tool_message("Q" * 40_000, call_id="c1"),
        ],
    }
    prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(n_ctx=1200, output_reserve=50, safety_margin=20),
        expected_model="Qwen3.5-9B-Q4_K_M",
    )
    assert calls == []


def test_observe_tag_attaches_another_slice(tmp_path):
    store = ObservationStore(tmp_path / "obs", ClipPolicy(excerpt_chars=120, head_chars=40, tail_chars=40))
    view = store.put("AAAA" + ("b" * 2000) + "ZZZZ", tool="read")
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [{"role": "user", "content": f"OBSERVE {view.observation_id} {view.next_offset}"}],
    }
    prepared, _ = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    attached = prepared["messages"][-1]["content"]
    assert "observation_id:" in attached
    assert len(attached) < 2000


def test_oversized_write_is_rejected_not_clipped():
    completion = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "w1",
                    "function": {"name": "write", "arguments": "x" * 250_000},
                }],
            }
        }]
    }
    out = reject_oversized_write(completion, ContextPolicy())
    message = out["choices"][0]["message"]
    assert message.get("tool_calls") == []
    assert "Rejected oversized file-writing" in message["content"]


def test_model_mismatch_refuses_dispatch(tmp_path):
    store = ObservationStore(tmp_path / "obs")
    with pytest.raises(ContextBudgetError, match="model mismatch"):
        prepare_chat_request(
            {"model": "Qwen3.5-4B-Q4_K_M", "messages": [{"role": "user", "content": "hi"}]},
            store=store, counter=ConservativeEstimator(),
            policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
        )


def test_account_does_not_treat_bytes_as_tokens():
    counter = ConservativeEstimator()
    text = "token token token token"
    body = {"messages": [{"role": "user", "content": text}]}
    counted = account(body, counter, ContextPolicy())
    assert counted.input_tokens != len(text.encode("utf-8"))
    assert counted.counter_label == "estimate"


def test_historical_overflow_shape_is_reproduced_separately(tmp_path):
    """Replay the 379 KB assistant-message shape without touching original evidence."""
    original_evidence = Path.home() / (
        ".local/share/local-coder/evidence/9b-taskboard-2026-09-17/create_session_summary.json"
    )
    if original_evidence.exists():
        before = original_evidence.read_bytes()
    store = ObservationStore(tmp_path / "obs")
    replay = "REPLAY-379KB-SHAPE\n" + ("/usr/lib/node_modules/pkg/index.js\n" * 12_000)
    assert len(replay.encode("utf-8")) >= 370_000
    body = {
        "model": "Qwen3.5-9B-Q4_K_M",
        "messages": [_assistant_call("hist"), _tool_message(replay, tool="read", call_id="hist")],
    }
    prepared, accounting = prepare_chat_request(
        body, store=store, counter=ConservativeEstimator(),
        policy=ContextPolicy(), expected_model="Qwen3.5-9B-Q4_K_M",
    )
    assert accounting.original_result_chars >= 370_000
    assert len(prepared["messages"][-1]["content"]) < 12_000
    if original_evidence.exists():
        assert original_evidence.read_bytes() == before


def test_llama_tokenizer_falls_back_to_scaling_estimate(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise OSError("offline")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("httpx.Client", Boom)
    tok = LlamaCppTokenizer("http://127.0.0.1:9")
    assert tok.count("z" * 800) > tok.count("z" * 40)
