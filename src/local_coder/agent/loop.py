"""Nicoder's agent loop: the model calls tools until it answers.

Tool calls use the OpenAI-compatible `tools` field, so any chat endpoint that
supports it can drive the loop. Nicoder runs every call itself
(tools.py); the model never touches files or a shell directly. When the
conversation outgrows the context, the oldest tool output is dropped first.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_coder.general.harness import strip_think

from .tools import ToolBox

ChatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]

DROPPED = "[Earlier tool output removed to save room. Run the tool again if you still need it.]"
_WRAP_UP = (
    "Stop using tools now. Reply to the user with what you found or changed so far, and what is still left, "
    "in a few plain sentences."
)
_NUDGE = "Please answer the user now, in plain words."
# An answer with no tool used at all, when the answer depends on the files: "Let me check what's on disk" or "nothing
# is there yet" with nothing read (Nico's France chat, 2026-10-26). Sent once; a second plain answer stands.
_LOOK_FIRST = (
    "You have not used any tool yet. If the answer depends on the project's files or on what you did, check "
    "with your tools first (list, read, run), then answer. Don't guess, don't promise to check, and never "
    "claim something you did not do."
)
_THANKS = re.compile(r"^(?:thanks|thank you|thx|ok|okay|cool|great|nice|perfect|got it|good)[\s!.]*$", re.I)
# A first answer that only promises to look ("Let me check what's on disk") never stands.
_PROMISE = re.compile(
    r"^(?:(?:sure|ok|okay|alright|now|great|right)[\s,!.]+)*"
    r"(?:let me|let's|let us|i['’]?ll|i will|i['’]m going to|i am going to|one moment|checking|"
    r"give me a (?:sec|second|moment))\b",
    re.I,
)
_HURRY = (
    "You have used much of this request's budget. Answer the user now with what you know; look further only "
    "if something essential is missing."
)
_KEEP_RECENT = 6
_RECENT_TOOL_CHARS = 4000
_LIMIT_TEXT = {
    "step_limit": "I reached the step limit for this request before finishing.",
    "time_limit": "I ran out of time for this request before finishing.",
}


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def plain_answer(text: str) -> str:
    """Headings become bold lines and tables become bullets, so a chat bubble shows them cleanly."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1).strip().strip("*").strip()
            out.append(f"**{title}**" if title else "")
            index += 1
            continue
        if line.strip().startswith("|") and index + 1 < len(lines) and _TABLE_RULE.match(lines[index + 1]):
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|") if cell.strip()]
                if cells:
                    out.append(f"- {cells[0]}" + (f" — {' — '.join(cells[1:])}" if cells[1:] else ""))
                index += 1
            continue
        out.append(line)
        index += 1
    return "\n".join(out).strip()


@dataclass
class AgentRun:
    """How one request ended. status: answered, stopped, step_limit, time_limit or error."""

    answer: str
    status: str
    steps: int = 0
    tool_calls: int = 0
    error: str = ""


def _message(reply: dict[str, Any]) -> dict[str, Any]:
    choices = reply.get("choices") if isinstance(reply, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


def _tool_calls(message: dict[str, Any], *, offset: int) -> list[dict[str, Any]]:
    """Normalized tool calls with ids, names and JSON-string arguments."""
    calls: list[dict[str, Any]] = []
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments)
        calls.append({
            "id": str(call.get("id") or f"call_{offset + index + 1}"),
            "type": "function",
            "function": {"name": name, "arguments": str(arguments or "{}")},
        })
    return calls


def _first_line(text: str, limit: int = 160) -> str:
    line = next((row.strip() for row in text.splitlines() if row.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


_IMAGE_SIZE = 6_000  # what one shown image counts for in the conversation's size (its data is not text)


def _size(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, list):
        size = sum(len(str(part.get("text") or "")) if part.get("type") == "text" else _IMAGE_SIZE
                   for part in content if isinstance(part, dict))
    else:
        size = len(str(content or ""))
    if message.get("tool_calls"):
        size += len(json.dumps(message["tool_calls"]))
    return size


def _without_images(message: dict[str, Any]) -> dict[str, Any]:
    """The message as a transcript keeps it: an image's data is left out."""
    content = message.get("content")
    if not isinstance(content, list):
        return message
    parts = [part if part.get("type") == "text" else {"type": "image_url", "image_url": {"url": "(image left out)"}}
             for part in content if isinstance(part, dict)]
    return {**message, "content": parts}


class AgentSession:
    """One conversation with the model and its tools. `ask` can be called again to continue it."""

    def __init__(
        self,
        *,
        chat: ChatFn,
        toolbox: ToolBox,
        system: str,
        history: list[dict[str, str]] | None = None,
        context_chars: int = 200_000,
        on_activity: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.chat = chat
        self.toolbox = toolbox
        self.context_chars = context_chars
        self.on_activity = on_activity
        self.on_step = on_step
        self.cancel = cancel
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.calls_log: list[dict[str, Any]] = []
        self.cost_usd: float | None = None
        for row in history or []:
            role = str(row.get("role") or "")
            content = str(row.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                self.messages.append({"role": role, "content": content})

    def _cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _activity(self, text: str, step: dict[str, Any] | None = None) -> None:
        """Report progress: a text line, and for apps a structured step (tool, target, ok)."""
        if text and self.on_activity is not None:
            self.on_activity(text)
        if self.on_step is not None and (text or step):
            self.on_step({"detail": text, **(step or {})})

    def ask(
        self,
        text: str,
        *,
        max_steps: int = 40,
        deadline: float | None = None,
        hurry_after: float | None = None,
        look_first: bool = False,
        images: list[str] | None = None,
    ) -> AgentRun:
        """Send one user message and run tools until the model answers or a limit is hit.

        With `hurry_after` (a fraction such as 0.5), the model is asked once to wrap up
        when that share of the steps or of the time is used. With `look_first`, an answer given without any
        tool call is sent back once with a request to check first. `images` (data URLs) are shown with the message.
        """
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
            parts += [{"type": "image_url", "image_url": {"url": url}} for url in images]
            self.messages.append({"role": "user", "content": parts})
        else:
            self.messages.append({"role": "user", "content": text})
        started = time.monotonic()
        limit = deadline if deadline is not None else started + 900
        steps = calls = 0
        nudged = hurried = looked = False
        look_first = look_first and not _THANKS.match(text.strip())
        first_answer = ""
        while True:
            if self._cancelled():
                return AgentRun("Stopped.", "stopped", steps, calls)
            if steps >= max_steps:
                return self._wrap_up("step_limit", steps, calls)
            now = time.monotonic()
            if now >= limit:
                return self._wrap_up("time_limit", steps, calls)
            if hurry_after is not None and not hurried and (
                steps >= max_steps * hurry_after or now - started >= (limit - started) * hurry_after
            ):
                hurried = True
                self.messages.append({"role": "user", "content": _HURRY})
            self._compact()
            call_started = time.monotonic()
            try:
                reply = self.chat(self.messages, self.toolbox.schemas())
            except Exception as exc:  # noqa: BLE001 — any provider failure ends this request plainly
                self._log_call(call_started, None, error=str(exc))
                if self._cancelled():
                    return AgentRun("Stopped.", "stopped", steps, calls)
                return AgentRun(f"The model did not answer: {exc}", "error", steps, calls, error=str(exc))
            self._log_call(call_started, reply)
            steps += 1
            message = _message(reply)
            content = strip_think(str(message.get("content") or "")).strip()
            tool_calls = _tool_calls(message, offset=calls)
            entry: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            self.messages.append(entry)
            if not tool_calls:
                if not content and looked and calls == 0 and first_answer and not _PROMISE.match(first_answer):
                    return AgentRun(plain_answer(first_answer), "answered", steps, calls)  # nothing new: it stands
                if content and look_first and calls == 0 and not looked:
                    looked = True
                    first_answer = content
                    self.messages.append({"role": "user", "content": _LOOK_FIRST})
                    continue
                if content and looked and calls == 0 and not _PROMISE.match(first_answer):
                    # Asked to check, it still used no tool: nothing needed checking, so its first answer stands.
                    return AgentRun(plain_answer(first_answer), "answered", steps, calls)
                if content:
                    return AgentRun(plain_answer(content), "answered", steps, calls)
                if nudged:
                    return AgentRun("The model finished without writing an answer.", "error", steps, calls)
                nudged = True
                self.messages.append({"role": "user", "content": _NUDGE})
                continue
            if content:
                self._activity(_first_line(content), {"tool": "note"})
            for call in tool_calls:
                name = call["function"]["name"]
                if self._cancelled():
                    result_text = "Stopped by the user before this tool ran."
                else:
                    outcome = self.toolbox.call(name, call["function"]["arguments"])
                    calls += 1
                    self._activity(outcome.activity, {"tool": name, "target": outcome.target, "ok": outcome.ok})
                    result_text = outcome.text
                self.messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "name": name, "content": result_text},
                )

    def _log_call(self, started: float, reply: dict[str, Any] | None, *, error: str = "") -> None:
        """Per-call timing and token use for the transcript (shows a slow provider at a glance)."""
        row: dict[str, Any] = {"seconds": round(time.monotonic() - started, 2)}
        if reply is not None:
            usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
            timings = reply.get("timings") if isinstance(reply.get("timings"), dict) else {}
            details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
            row.update({
                "prompt_tokens": (usage or {}).get("prompt_tokens"),
                "completion_tokens": (usage or {}).get("completion_tokens"),
                "reasoning_tokens": (details or {}).get("reasoning_tokens") if isinstance(details, dict) else None,
                "first_token_s": (timings or {}).get("first_token_s"),
                "attempts": (timings or {}).get("attempts"),
            })
            cost = (usage or {}).get("cost_usd")
            if isinstance(cost, (int, float)):
                row["cost_usd"] = cost
                self.cost_usd = round((self.cost_usd or 0.0) + float(cost), 6)
        if error:
            row["error"] = error[:300]
        self.calls_log.append(row)

    def _wrap_up(self, status: str, steps: int, calls: int) -> AgentRun:
        """One last call asking for a summary; any tool calls in it are not run."""
        self.messages.append({"role": "user", "content": _WRAP_UP})
        self._compact()
        content = ""
        started = time.monotonic()
        try:
            reply = self.chat(self.messages, self.toolbox.schemas())
            self._log_call(started, reply)
            content = strip_think(str(_message(reply).get("content") or "")).strip()
        except Exception:  # noqa: BLE001 — the limit text below is the answer then
            content = ""
        self.messages.append({"role": "assistant", "content": content})
        return AgentRun(plain_answer(content) or _LIMIT_TEXT[status], status, steps + 1, calls)

    def _compact(self) -> None:
        """Drop the oldest tool output, then clip recent output, until the conversation fits."""
        total = sum(_size(message) for message in self.messages)
        if total <= self.context_chars:
            return
        protected = max(1, len(self.messages) - _KEEP_RECENT)
        for message in self.messages[:protected]:
            if message.get("role") == "tool" and message.get("content") != DROPPED:
                total -= len(str(message.get("content") or "")) - len(DROPPED)
                message["content"] = DROPPED
                if total <= self.context_chars:
                    return
        for message in self.messages[protected:]:
            content = str(message.get("content") or "")
            if message.get("role") == "tool" and len(content) > _RECENT_TOOL_CHARS:
                message["content"] = content[:_RECENT_TOOL_CHARS] + "\n… (cut to save room)"

    def save(self, path: Path, **extra: Any) -> Path:
        """Write the transcript (system prompt, messages, tool results) as evidence."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **extra,
            "read": self.toolbox.read_paths,
            "changed": self.toolbox.changed_paths,
            "commands": self.toolbox.commands,
            "calls": self.calls_log,
            "messages": [_without_images(message) for message in self.messages],
        }
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return path
