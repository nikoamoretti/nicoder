"""OpenAI-compatible chat streaming contract used by OpenCode 1.18.22.

OpenCode's llama.cpp provider is `@ai-sdk/openai-compatible` / streamText.
It reads `choices[].delta` chunks, not a full `chat.completion` object stuffed
into one `data:` line.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class StreamContractError(Exception):
    """The stream was an error or could not be assembled."""

    def __init__(self, detail: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.payload = payload or {}


@dataclass
class AssembledCompletion:
    """Result of assembling OpenAI chat.completion.chunk events."""

    role: str = "assistant"
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None
    completion_id: str | None = None
    error: dict[str, Any] | None = None

    def as_completion(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        payload: dict[str, Any] = {
            "id": self.completion_id or "chatcmpl-local",
            "object": "chat.completion",
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason,
            }],
        }
        if self.usage:
            payload["usage"] = self.usage
        return payload


def completion_to_chunks(completion: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate one non-streaming completion into valid streaming chunks."""
    if completion.get("error"):
        raise StreamContractError("completion is an error", completion)
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        raise StreamContractError("completion has no choices", completion)
    choice = _as_dict(choices[0])
    message = _as_dict(choice.get("message"))
    base = {
        "id": completion.get("id") or "chatcmpl-local",
        "object": "chat.completion.chunk",
        "created": completion.get("created"),
        "model": completion.get("model"),
    }
    chunks: list[dict[str, Any]] = [{
        **base,
        "choices": [{"index": 0, "delta": {"role": message.get("role") or "assistant"}, "finish_reason": None}],
    }]
    content = message.get("content")
    if isinstance(content, str) and content:
        chunks.append({
            **base,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        })
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        fn = _as_dict(call.get("function"))
        chunks.append({
            **base,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": index,
                "id": call.get("id"),
                "type": call.get("type") or "function",
                "function": {"name": fn.get("name") or "", "arguments": ""},
            }]}, "finish_reason": None}],
        })
        args = fn.get("arguments")
        raw = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
        if raw:
            chunks.append({
                **base,
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": index,
                    "function": {"arguments": raw},
                }]}, "finish_reason": None}],
            })
    finish: dict[str, Any] = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
    }
    if completion.get("usage"):
        finish["usage"] = completion["usage"]
    chunks.append(finish)
    return chunks


def chunks_to_sse(chunks: list[dict[str, Any]]) -> bytes:
    """Encode chunks as text/event-stream with a terminal [DONE]."""
    parts = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


def error_to_sse(error: dict[str, Any]) -> bytes:
    """Encode a provider error as an SSE error event. Not a successful completion."""
    return f"data: {json.dumps({'error': error}, ensure_ascii=False)}\n\n".encode()


def parse_sse_block(block: str) -> dict[str, Any] | str | None:
    """Parse one SSE event body. Returns a chunk dict, '[DONE]', or None."""
    data_lines: list[str] = []
    for line in block.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    raw = "\n".join(data_lines).strip()
    if raw == "[DONE]":
        return "[DONE]"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise StreamContractError(f"SSE data was not JSON: {raw[:200]}") from exc
    if not isinstance(payload, dict):
        raise StreamContractError("SSE data was not an object")
    return payload


def assemble_chunks(chunks: list[dict[str, Any]]) -> AssembledCompletion:
    """Assemble OpenAI-compatible deltas the way OpenCode/AI SDK consumes them."""
    assembled = AssembledCompletion()
    calls: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        if chunk.get("error"):
            assembled.error = chunk["error"] if isinstance(chunk["error"], dict) else {"message": str(chunk["error"])}
            raise StreamContractError(str(assembled.error.get("message") or assembled.error), assembled.error)
        if "choices" not in chunk and "delta" not in chunk:
            if chunk.get("object") == "chat.completion":
                raise StreamContractError(
                    "received a full chat.completion object where a chunk was required"
                )
        assembled.model = assembled.model or chunk.get("model")
        assembled.completion_id = assembled.completion_id or chunk.get("id")
        if chunk.get("usage"):
            assembled.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if delta is None and choice.get("message"):
                raise StreamContractError(
                    "choice has message but no delta; this is not a streaming chunk"
                )
            if not isinstance(delta, dict):
                continue
            if delta.get("role"):
                assembled.role = str(delta["role"])
            if isinstance(delta.get("content"), str):
                assembled.content += delta["content"]
            for item in delta.get("tool_calls") or []:
                if not isinstance(item, dict):
                    continue
                index = int(item.get("index") or 0)
                slot = calls.setdefault(index, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                    "index": index,
                })
                if item.get("id"):
                    slot["id"] = str(item["id"])
                if item.get("type"):
                    slot["type"] = str(item["type"])
                fn = _as_dict(item.get("function"))
                if fn.get("name"):
                    slot["function"]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    slot["function"]["arguments"] += str(fn["arguments"])
            if choice.get("finish_reason"):
                assembled.finish_reason = str(choice["finish_reason"])
    assembled.tool_calls = [calls[i] for i in sorted(calls)]
    return assembled


def looks_like_chunk(payload: dict[str, Any]) -> bool:
    """True when the object is a streaming chunk, not a full completion."""
    if payload.get("object") == "chat.completion.chunk":
        return True
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return "delta" in choices[0] and "message" not in choices[0]
    return False


class ToolCallAccumulator:
    """Track streamed tool-call arguments so oversized writes can be refused."""

    def __init__(self) -> None:
        self.calls: dict[int, dict[str, Any]] = {}

    def ingest(self, chunk: dict[str, Any]) -> None:
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = _as_dict(choice.get("delta"))
            for item in delta.get("tool_calls") or []:
                if not isinstance(item, dict):
                    continue
                index = int(item.get("index") or 0)
                slot = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if item.get("id"):
                    slot["id"] = str(item["id"])
                fn = _as_dict(item.get("function"))
                if fn.get("name"):
                    slot["name"] += str(fn["name"])
                if fn.get("arguments"):
                    slot["arguments"] += str(fn["arguments"])
