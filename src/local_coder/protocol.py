"""Strict versioned four-action model protocol.

The local model sees exactly FIND, PATCH, CHECK, DONE. Each generation must
return exactly one JSON object. Unknown fields are rejected. Markdown fences,
action lists, and multi-action plans are rejected. No inference-based repair
happens here: invalid output consumes a model decision and yields a compact
protocol error for the next turn.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL_VERSION = "1"

MAX_DIFF_CHARS = 32 * 1024
MAX_RAW_CHARS = MAX_DIFF_CHARS + 2048


class ProtocolError(Exception):
    """Raw model output is not exactly one valid action."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class FindAction(BaseModel):
    """Locate relevant code. The runtime owns search and ranking."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["find"]
    query: str = Field(min_length=1, max_length=500)
    symbols: list[str] = Field(default_factory=list, max_length=20)
    hypothesis: str | None = Field(default=None, max_length=500)


class PatchApplyAction(BaseModel):
    """Apply an ordinary unified diff to current focus files."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["patch"]
    operation: Literal["apply"]
    diff: str = Field(min_length=1, max_length=MAX_DIFF_CHARS)
    expected_effect: str = Field(min_length=1, max_length=500)
    hypothesis: str | None = Field(default=None, max_length=500)


class PatchRevertAction(BaseModel):
    """Restore the runtime-recorded pre-patch bytes of the last patch."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["patch"]
    operation: Literal["revert_last"]
    reason_code: str = Field(min_length=1, max_length=100)
    hypothesis: str | None = Field(default=None, max_length=500)


class CheckAction(BaseModel):
    """Run one registered test scope. The model never picks a command."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["check"]
    scope: Literal["focused", "regression"]
    hypothesis: str | None = Field(default=None, max_length=500)


class DoneSolvedAction(BaseModel):
    """Request final validation. Solved is asserted only by the runtime."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["done"]
    status: Literal["solved"]
    summary: str = Field(min_length=1, max_length=1000)


class DoneBlockedAction(BaseModel):
    """Stop with a handoff. Always accepted when the episode is running."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["done"]
    status: Literal["blocked"]
    reason_code: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)


Action = FindAction | PatchApplyAction | PatchRevertAction | CheckAction | DoneSolvedAction | DoneBlockedAction


def _schema_error(action: str, exc: ValidationError) -> ProtocolError:
    first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid")
    return ProtocolError(f"invalid {action} action ({loc}: {msg})", "invalid_schema")


def parse_raw(raw: str | None) -> Action:
    """Parse exactly one action object from raw model output."""
    if raw is None or not raw.strip():
        raise ProtocolError("empty model output", "empty")
    text = raw.strip()
    if len(text) > MAX_RAW_CHARS:
        raise ProtocolError("model output exceeds size budget", "too_large")
    if "```" in text:
        raise ProtocolError("markdown fence in output; return exactly one JSON object", "markdown_fence")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg} at position {exc.pos}", "invalid_json") from exc
    if isinstance(data, list):
        raise ProtocolError("multiple actions returned; return exactly one JSON object", "multiple_actions")
    if not isinstance(data, dict):
        raise ProtocolError("model output must be a JSON object", "not_object")
    name = data.get("action")
    try:
        if name == "find":
            return FindAction.model_validate(data)
        if name == "patch":
            if data.get("operation") == "revert_last":
                return PatchRevertAction.model_validate(data)
            return PatchApplyAction.model_validate(data)
        if name == "check":
            return CheckAction.model_validate(data)
        if name == "done":
            if data.get("status") == "blocked":
                return DoneBlockedAction.model_validate(data)
            return DoneSolvedAction.model_validate(data)
    except ValidationError as exc:
        raise _schema_error(str(name), exc) from exc
    raise ProtocolError(f"unknown action {name!r}; expected find|patch|check|done", "unknown_action")


def fingerprint(action: Action, context_hash: str) -> str:
    """Stable identity for repetition detection.

    Free-text fields (hypothesis, expected_effect, summary) are excluded so
    rewording the same move does not look like progress. The context hash
    (candidate tree state, and for checks the scope-relevant state) makes the
    same move on changed code distinct.
    """
    data = action.model_dump()
    for free in ("hypothesis", "expected_effect", "summary"):
        data.pop(free, None)
    if isinstance(action, PatchApplyAction):
        data["diff_sha"] = hashlib.sha256(action.diff.encode("utf-8")).hexdigest()
        data.pop("diff", None)
    payload = {"action": data, "context": context_hash}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars//4), always labeled ESTIMATE at call sites."""
    return max(1, len(text) // 4)
