"""Account for a complete outgoing inference request before dispatch.

The check is tokens, not bytes. Estimates are labeled. A stub that always
returns a small count is refused by tests because counts must scale.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .observations import (
    ClipPolicy,
    ObservationStore,
    looks_like_tool_message,
    message_text,
    set_message_text,
)


class TokenCounter(Protocol):
    """Count tokens for request accounting."""

    labeled: str

    def count(self, text: str) -> int:
        """Return a token count that grows with the input."""
        ...


@dataclass
class ConservativeEstimator:
    """Overestimate from UTF-8 size. Used when the live tokenizer is unavailable."""

    labeled: str = "estimate"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text.encode("utf-8")) + 1) // 2)


@dataclass
class LlamaCppTokenizer:
    """Count via llama.cpp POST /tokenize when the owned server is up."""

    endpoint: str
    labeled: str = "llama.cpp /tokenize"

    def count(self, text: str) -> int:
        if not text:
            return 0
        import httpx

        try:
            with httpx.Client(trust_env=False, timeout=8.0) as client:
                response = client.post(
                    f"{self.endpoint.rstrip('/')}/tokenize",
                    json={"content": text, "add_special": False},
                )
            if response.status_code != 200:
                self.labeled = "estimate"
                return ConservativeEstimator().count(text)
            payload = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            self.labeled = "estimate"
            return ConservativeEstimator().count(text)
        tokens = payload.get("tokens")
        if isinstance(tokens, list):
            self.labeled = "llama.cpp /tokenize"
            return len(tokens)
        self.labeled = "estimate"
        return ConservativeEstimator().count(text)


@dataclass(frozen=True)
class ContextPolicy:
    """Runtime context capacity and reserves. Does not raise the window."""

    n_ctx: int = 32768
    output_reserve: int = 2048
    safety_margin: int = 512
    template_overhead: int = 400
    per_message_overhead: int = 8
    aggregate_tool_chars: int = 12_000
    clip: ClipPolicy = field(default_factory=ClipPolicy)

    def max_input_tokens(self) -> int:
        """Largest allowed prompt, after output reserve and safety."""
        return self.n_ctx - self.output_reserve - self.safety_margin


@dataclass
class RequestAccounting:
    """Before/after numbers for one assembled provider request."""

    allowed: bool
    reason: str
    input_tokens: int
    output_reserve: int
    safety_margin: int
    template_overhead: int
    remaining_headroom: int
    n_ctx: int
    counter_label: str
    original_result_chars: int = 0
    provider_visible_chars: int = 0
    stored_path: str | None = None
    compacted: bool = False
    output_source: str = "configured_default"
    estimated: bool = False


class ContextBudgetError(Exception):
    """The request cannot be dispatched without overflowing the runtime context."""

    def __init__(self, detail: str, accounting: RequestAccounting) -> None:
        super().__init__(detail)
        self.detail = detail
        self.accounting = accounting
        self.code = "context_budget"


def flatten_request(body: dict[str, Any]) -> str:
    """Concatenate the fields that consume context in an OpenAI-style request."""
    parts: list[str] = []
    for message in body.get("messages") or []:
        if isinstance(message, dict):
            parts.append(message_text(message))
            calls = message.get("tool_calls")
            if calls:
                parts.append(json.dumps(calls, ensure_ascii=False))
    tools = body.get("tools")
    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))
    if body.get("system"):
        parts.append(str(body["system"]))
    return "\n".join(parts)


def count_request(body: dict[str, Any], counter: TokenCounter, policy: ContextPolicy) -> int:
    """Count the complete input, including tool definitions and template slack."""
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    raw = counter.count(flatten_request(body))
    return raw + policy.template_overhead + policy.per_message_overhead * len(messages)


def account(body: dict[str, Any], counter: TokenCounter, policy: ContextPolicy) -> RequestAccounting:
    """Measure a request against the served context window."""
    input_tokens = count_request(body, counter, policy)
    ceiling = policy.max_input_tokens()
    remaining = policy.n_ctx - input_tokens - policy.output_reserve - policy.safety_margin
    allowed = input_tokens <= ceiling
    reason = "fits" if allowed else (
        f"complete input {input_tokens} + output reserve {policy.output_reserve} + "
        f"safety {policy.safety_margin} exceeds n_ctx {policy.n_ctx}"
    )
    return RequestAccounting(
        allowed=allowed,
        reason=reason,
        input_tokens=input_tokens,
        output_reserve=policy.output_reserve,
        safety_margin=policy.safety_margin,
        template_overhead=policy.template_overhead,
        remaining_headroom=remaining,
        n_ctx=policy.n_ctx,
        counter_label=counter.labeled,
        estimated=counter.labeled == "estimate",
    )


def resolve_output_limit(body: dict[str, Any], policy: ContextPolicy) -> tuple[int, str]:
    """Return the output allowance that will actually be sent to llama.cpp."""
    raw_tokens = body.get("max_tokens")
    raw_completion = body.get("max_completion_tokens")
    parsed_tokens = _positive_int(raw_tokens)
    parsed_completion = _positive_int(raw_completion)
    if parsed_tokens is not None and parsed_completion is not None and parsed_tokens != parsed_completion:
        return min(parsed_tokens, parsed_completion), "conflicting_max_tokens_used_min"
    if parsed_completion is not None:
        return parsed_completion, "max_completion_tokens"
    if parsed_tokens is not None:
        return parsed_tokens, "max_tokens"
    if raw_tokens is not None or raw_completion is not None:
        return policy.output_reserve, "invalid_nonpositive_used_default"
    return policy.output_reserve, "configured_default"


def _positive_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def prepare_chat_request(
    body: dict[str, Any],
    *,
    store: ObservationStore,
    counter: TokenCounter,
    policy: ContextPolicy,
    expected_model: str,
) -> tuple[dict[str, Any], RequestAccounting]:
    """Bound, compact if needed, then refuse before dispatch if still too large."""
    prepared = json.loads(json.dumps(body))
    _require_model(prepared, expected_model)
    _bound_tool_defs(prepared, store, policy)
    original_chars, visible_chars, stored = _bound_messages(prepared, store, policy)
    requested_out, out_source = resolve_output_limit(prepared, policy)
    measured = account(prepared, counter, replace(policy, output_reserve=requested_out))
    compacted = False
    if not measured.allowed:
        prepared, compacted = _compact_history(prepared, store, policy)
        measured = account(prepared, counter, replace(policy, output_reserve=requested_out))
        measured.compacted = compacted
    room = policy.n_ctx - measured.input_tokens - policy.safety_margin
    if room < 1:
        measured.allowed = False
        measured.reason = (
            f"irreducible context: complete input {measured.input_tokens} + "
            f"safety {policy.safety_margin} exceeds n_ctx {policy.n_ctx}. "
            "No generation was dispatched."
        )
        raise ContextBudgetError(measured.reason, measured)
    effective_out = min(requested_out, room)
    if effective_out < requested_out:
        out_source = f"{out_source};capped_to_remaining_context"
    prepared["max_tokens"] = effective_out
    if "max_completion_tokens" in prepared:
        prepared["max_completion_tokens"] = effective_out
    measured = account(prepared, counter, replace(policy, output_reserve=effective_out))
    measured.original_result_chars = original_chars
    measured.provider_visible_chars = visible_chars
    measured.stored_path = stored
    measured.output_source = out_source
    measured.estimated = counter.labeled == "estimate"
    measured.compacted = compacted
    if not measured.allowed:
        measured.reason = (
            f"irreducible context: {measured.reason}. "
            "No generation was dispatched."
        )
        raise ContextBudgetError(measured.reason, measured)
    return prepared, measured


@dataclass
class CompletionGate:
    """Whether a completion may be returned as a successful model response."""

    allowed: bool
    completion: dict[str, Any] | None
    error: dict[str, Any] | None = None
    evidence_path: str | None = None
    original_action: dict[str, Any] | None = None


WRITE_TOOLS = frozenset({"write", "edit", "write_file", "apply_patch"})


def gate_completion(
    completion: dict[str, Any],
    policy: ContextPolicy,
    store: ObservationStore | None = None,
) -> CompletionGate:
    """Refuse an oversized write as a provider error, keeping the original action."""
    if completion.get("error"):
        err = completion["error"]
        return CompletionGate(False, None, err if isinstance(err, dict) else {"message": str(err)})
    choices = completion.get("choices")
    if not isinstance(choices, list):
        return CompletionGate(True, completion)
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            raw_fn = call.get("function")
            fn = raw_fn if isinstance(raw_fn, dict) else {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments")
            raw = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
            if name in WRITE_TOOLS and len(raw) > policy.clip.write_arg_chars:
                evidence = None
                if store is not None:
                    view = store.put(raw, tool=name, status="rejected_oversized_write")
                    evidence = view.path
                error = {
                    "type": "oversized_write_rejected",
                    "code": "oversized_write",
                    "message": (
                        f"Rejected oversized {name} arguments "
                        f"({len(raw)} > {policy.clip.write_arg_chars}). "
                        "No partial write was applied."
                    ),
                    "tool": name,
                    "tool_call_id": call.get("id"),
                    "argument_chars": len(raw),
                    "limit_chars": policy.clip.write_arg_chars,
                    "evidence_path": evidence,
                }
                return CompletionGate(False, None, error, evidence, json.loads(json.dumps(call)))
    return CompletionGate(True, completion)


def reject_oversized_write(completion: dict[str, Any], policy: ContextPolicy) -> dict[str, Any]:
    """Compatibility wrapper. Prefer gate_completion for new callers."""
    gate = gate_completion(completion, policy)
    if gate.allowed and gate.completion is not None:
        return gate.completion
    return {"error": gate.error, "choices": []}


def _require_model(body: dict[str, Any], expected: str) -> None:
    model = str(body.get("model") or "")
    if model and model != expected and not model.endswith(f"/{expected}"):
        raise ContextBudgetError(
            f"model mismatch: request {model!r} is not the configured {expected!r}",
            RequestAccounting(
                allowed=False,
                reason="model_mismatch",
                input_tokens=0,
                output_reserve=0,
                safety_margin=0,
                template_overhead=0,
                remaining_headroom=0,
                n_ctx=0,
                counter_label="n/a",
            ),
        )
    if not model:
        body["model"] = expected


def _bound_messages(
    body: dict[str, Any],
    store: ObservationStore,
    policy: ContextPolicy,
) -> tuple[int, int, str | None]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0, 0, None
    original = 0
    visible = 0
    stored_path: str | None = None
    tool_chars = 0
    for index, message in enumerate(list(messages)):
        if not isinstance(message, dict):
            continue
        text = message_text(message)
        original += len(text)
        role = str(message.get("role") or "")
        may_clip = looks_like_tool_message(message) or (
            role == "assistant" and _oversized_plain(text, policy) and not message.get("tool_calls")
        )
        if may_clip:
            view = store.put(
                text,
                tool=_tool_name(message),
                status="ok",
                exit_code=_exit_code(text),
            )
            stored_path = stored_path or view.path
            rendered = view.as_text()
            if tool_chars + len(rendered) > policy.aggregate_tool_chars and looks_like_tool_message(message):
                rendered = (
                    f"tool: {view.tool}\nstatus: omitted_for_aggregate_budget\n"
                    f"observation_id: {view.observation_id}\n"
                    f"more: run local-coder-observe {view.observation_id} 0\n"
                )
            tool_chars += len(rendered)
            messages[index] = set_message_text(message, rendered)
            visible += len(rendered)
        else:
            visible += len(text)
    return original, visible, stored_path


def _bound_tool_defs(
    body: dict[str, Any],
    store: ObservationStore,
    policy: ContextPolicy,
) -> None:
    """Clip oversized tool descriptions. Keep names so calls still bind."""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(fn, dict):
            continue
        desc = fn.get("description")
        if isinstance(desc, str) and len(desc) > policy.clip.excerpt_chars:
            view = store.put(desc, tool=str(fn.get("name") or "tool"), status="clipped_description")
            fn["description"] = view.as_text()[: policy.clip.excerpt_chars]


def _oversized_plain(text: str, policy: ContextPolicy) -> bool:
    return len(text) > policy.clip.excerpt_chars * 2


def _tool_name(message: dict[str, Any]) -> str:
    name = message.get("name") or message.get("tool") or message.get("role") or "tool"
    return str(name)


def _exit_code(text: str) -> int | None:
    match = None
    for line in text.splitlines()[:20]:
        if line.lower().startswith("exit:") or line.lower().startswith("exit_code"):
            bits = line.replace("=", " ").split()
            for bit in reversed(bits):
                if bit.lstrip("-").isdigit():
                    return int(bit)
            match = 1
    return match


def _compact_history(
    body: dict[str, Any],
    store: ObservationStore,
    policy: ContextPolicy,
) -> tuple[dict[str, Any], bool]:
    """Stub oldest completed tool observations. Keep ids and the latest user text."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body, False
    compacted = False
    latest_user = max(
        (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"),
        default=-1,
    )
    for index, message in enumerate(messages):
        if index == latest_user or not isinstance(message, dict):
            continue
        if not looks_like_tool_message(message):
            continue
        text = message_text(message)
        if "observation_id:" in text and len(text) <= 400:
            continue
        view = store.put(text, tool=_tool_name(message), status="compacted")
        stub = (
            f"tool: {view.tool}\nstatus: compacted\n"
            f"observation_id: {view.observation_id}\n"
            "excerpt: [previous tool output compacted; ids preserved]\n"
        )
        messages[index] = set_message_text(message, stub)
        compacted = True
    return body, compacted
