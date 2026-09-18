"""Bounded tool observations with full evidence stored outside Git.

The model sees a clipped excerpt. The complete payload stays in the
app-owned data directory under a finite disk budget.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .opencode_config import data_root

ERROR_LINE = re.compile(r"(error|exception|failed|fatal|traceback|denied)", re.IGNORECASE)
OBSERVE_TAG = re.compile(r"OBSERVE\s+([A-Za-z0-9_-]+)\s+(\d+)")


@dataclass(frozen=True)
class ObservationView:
    """What the model is allowed to see of one stored result."""

    observation_id: str
    tool: str
    status: str
    exit_code: int | None
    excerpt: str
    omitted: bool
    omitted_bytes: int
    offset: int
    next_offset: int | None
    path: str

    def as_text(self) -> str:
        """Render a structured, boundary-safe observation for the model."""
        lines = [
            f"tool: {self.tool}",
            f"status: {self.status}",
        ]
        if self.exit_code is not None:
            lines.append(f"exit_code: {self.exit_code}")
        lines.append(f"observation_id: {self.observation_id}")
        lines.append(f"offset: {self.offset}")
        if self.omitted:
            lines.append(f"omitted_bytes: {self.omitted_bytes}")
            if self.next_offset is not None:
                lines.append(
                    f"more: run local-coder-observe {self.observation_id} {self.next_offset}"
                )
            else:
                lines.append("more: none")
        lines.append("excerpt:")
        lines.append(self.excerpt)
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ClipPolicy:
    """Per-result and store limits. Token budgets live in context_budget."""

    excerpt_chars: int = 4000
    head_chars: int = 1200
    tail_chars: int = 2000
    error_window: int = 800
    disk_budget_bytes: int = 64 * 1024 * 1024
    write_arg_chars: int = 200_000


def observation_root() -> Path:
    """App-owned evidence directory (not the user's project, not Git)."""
    path = data_root() / "tool-observations"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ObservationStore:
    """Persist full tool payloads and hand back bounded slices."""

    def __init__(self, root: Path | None = None, policy: ClipPolicy | None = None) -> None:
        self.root = root or observation_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or ClipPolicy()

    def put(
        self,
        payload: str,
        *,
        tool: str,
        status: str = "ok",
        exit_code: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ObservationView:
        """Store the full payload and return the first bounded view."""
        self._enforce_disk_budget()
        oid = f"obs_{uuid.uuid4().hex[:16]}"
        raw = payload.encode("utf-8")
        folder = self.root / oid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "full.txt").write_bytes(raw)
        status = observation_status(exit_code=exit_code, payload=payload, requested=status)
        record = {
            "observation_id": oid,
            "tool": tool,
            "status": status,
            "exit_code": exit_code,
            "bytes": len(raw),
            "chars": len(payload),
            "created_ts": time.time(),
            "meta": meta or {},
        }
        (folder / "meta.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return self.read_slice(oid, offset=0)

    def read_slice(self, observation_id: str, offset: int = 0, limit: int | None = None) -> ObservationView:
        """Return one bounded portion. Never the entire oversized payload."""
        folder = self.root / observation_id
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        text = (folder / "full.txt").read_text(encoding="utf-8")
        start = max(0, min(offset, len(text)))
        budget = limit if limit is not None else self.policy.excerpt_chars
        budget = max(1, min(budget, self.policy.excerpt_chars))
        excerpt, omitted_here = clip_text(text[start:], self.policy, budget=budget)
        consumed = min(len(text) - start, _consumed_chars(text[start:], excerpt, budget))
        next_offset = start + consumed if start + consumed < len(text) else None
        omitted = start > 0 or next_offset is not None or omitted_here
        return ObservationView(
            observation_id=observation_id,
            tool=str(meta.get("tool") or "unknown"),
            status=str(meta.get("status") or "ok"),
            exit_code=meta.get("exit_code"),
            excerpt=excerpt,
            omitted=omitted,
            omitted_bytes=max(0, len(text.encode("utf-8")) - len(excerpt.encode("utf-8"))),
            offset=start,
            next_offset=next_offset,
            path=str(folder / "full.txt"),
        )

    def path_for(self, observation_id: str) -> Path:
        """Absolute path of the preserved full payload."""
        return self.root / observation_id / "full.txt"

    def _enforce_disk_budget(self) -> None:
        rows: list[tuple[float, Path, int]] = []
        total = 0
        for folder in self.root.iterdir():
            full = folder / "full.txt"
            if not full.exists():
                continue
            size = full.stat().st_size
            total += size
            rows.append((full.stat().st_mtime, folder, size))
        rows.sort()
        while total > self.policy.disk_budget_bytes and rows:
            _, folder, size = rows.pop(0)
            for child in folder.iterdir():
                child.unlink(missing_ok=True)
            folder.rmdir()
            total -= size


def clip_text(text: str, policy: ClipPolicy, budget: int | None = None) -> tuple[str, bool]:
    """Keep head, nearby error lines, and tail without splitting code points."""
    limit = budget if budget is not None else policy.excerpt_chars
    if len(text) <= limit:
        return text, False
    head = text[: policy.head_chars]
    tail = text[-policy.tail_chars :]
    error = _error_excerpt(text, policy.error_window)
    parts = [head]
    if error and error not in head and error not in tail:
        parts.append("\n… [error context] …\n")
        parts.append(error)
    parts.append("\n… [omitted] …\n")
    parts.append(tail)
    joined = "".join(parts)
    if len(joined) > limit:
        joined = joined[:limit]
    return joined, True


def _error_excerpt(text: str, window: int) -> str:
    last = ""
    for match in ERROR_LINE.finditer(text):
        start = max(0, match.start() - 80)
        last = text[start : match.start() + window]
    return last


def _consumed_chars(remaining: str, excerpt: str, budget: int) -> int:
    if len(remaining) <= budget:
        return len(remaining)
    return min(len(remaining), max(budget, len(excerpt)))


def find_observe_requests(text: str) -> list[tuple[str, int]]:
    """Parse legacy OBSERVE tags. Automatic attachment must not use this."""
    return [(m.group(1), int(m.group(2))) for m in OBSERVE_TAG.finditer(text)]


def observation_status(*, exit_code: int | None, payload: str, requested: str = "ok") -> str:
    """Preserve genuine failure. A nonzero exit is never an unqualified ok."""
    if requested in {"error", "failed", "compacted", "clipped_description", "omitted_for_aggregate_budget"}:
        return requested
    if exit_code not in (None, 0):
        return "error"
    head = payload[:2_000].lower()
    if "exit_code: 0" in head or head.startswith("exit: 0"):
        return "ok"
    if ERROR_LINE.search(payload[:2_000]) and ("exit_code" in head or head.startswith("exit:")):
        return "error"
    return requested


def is_observe_tool(message: dict[str, Any]) -> bool:
    """True when this tool result is an explicit local-coder-observe call."""
    name = str(message.get("name") or message.get("tool") or "")
    return name in {"local_coder_observe", "local-coder-observe"}


def looks_like_tool_message(message: dict[str, Any]) -> bool:
    """True when a chat message is a tool result OpenCode would serialize."""
    role = str(message.get("role") or "")
    if role == "tool":
        return True
    if message.get("tool_call_id") or message.get("tool_calls"):
        return role in {"tool", "function"}
    content = message.get("content")
    if isinstance(content, str) and content.startswith("<tool_response>"):
        return True
    return False


def message_text(message: dict[str, Any]) -> str:
    """Flatten OpenAI-style content to text without dropping unicode."""
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def set_message_text(message: dict[str, Any], text: str) -> dict[str, Any]:
    """Replace message content while preserving role and tool-call ids."""
    updated = dict(message)
    content = message.get("content")
    if isinstance(content, list):
        updated["content"] = [{"type": "text", "text": text}]
    else:
        updated["content"] = text
    return updated
