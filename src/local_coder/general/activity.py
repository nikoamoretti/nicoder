"""Plain-language activity from OpenCode events and session messages."""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Soft status lines — fine as a headline, but not useful as an Actions entry.
VAGUE_ACTIVITY = {
    "the model is thinking about the next step.",
    "the model is working.",
    "waiting for the model to start.",
    "waiting for the model’s next tool call.",
    "waiting for the model's next tool call.",
}


def _state(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def is_vague_activity(phrase: str) -> bool:
    """True for soft waiting lines that should not fill the Actions list."""
    return phrase.strip().lower() in VAGUE_ACTIVITY


def short_path(value: object) -> str:
    """Return a short file label, or empty if there is nothing useful."""
    text = str(value or "").strip()
    if not text:
        return ""
    name = Path(text).name
    parent = Path(text).parent.name
    if parent and parent not in {".", "/", ""} and name in {"package.json", "index.ts", "index.tsx", "main.tsx"}:
        return f"{parent}/{name}"
    if name:
        return name
    return text[-40:]


def describe_tool(tool: str, state: dict[str, Any] | None) -> str:
    """One sentence for a tool call, including the command or file."""
    state = state or {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    status = str(state.get("status") or "")
    title = str(state.get("title") or "").strip()
    name = (tool or "").lower()
    path = inp.get("filePath") or inp.get("path") or inp.get("file")
    command = str(inp.get("command") or inp.get("cmd") or "").strip()
    done = status == "completed"
    running = status in {"running", "pending", "executing", ""}
    if name in {"bash", "shell"}:
        cmd = " ".join((command or title).split())
        if len(cmd) > 88:
            cmd = cmd[:85] + "..."
        if cmd:
            return f"{'Ran' if done else 'Running'}: {cmd}"
        return "Ran a command." if done else "Running a command."
    if name in {"read", "view"}:
        label = short_path(path) or short_path(title) or "a file"
        return f"{'Read' if done else 'Reading'} {label}"
    if name in {"write", "edit", "multiedit"}:
        label = short_path(path) or short_path(title) or "a file"
        return f"{'Wrote' if done else 'Writing'} {label}"
    if name in {"glob", "grep", "search"}:
        query = str(inp.get("pattern") or inp.get("query") or title or name)
        if len(query) > 72:
            query = query[:69] + "..."
        return f"Searching for {query}"
    if name in {"todowrite", "todoread", "todo"}:
        return "Updating the task list." if done else "Planning the next tasks."
    if name:
        extra = short_path(path) or command or title
        if extra:
            prefix = "Finished" if done else ("Using" if running else "Used")
            return f"{prefix} {name} on {extra}"
        return f"{'Finished' if done else 'Using'} {name}."
    return ""


def _part_phrase(part: dict[str, Any]) -> str:
    """One concrete phrase from a message part, or empty."""
    ptype = str(part.get("type") or "")
    if ptype == "tool":
        return describe_tool(str(part.get("tool") or ""), _state(part.get("state")))
    if ptype in {"reasoning", "thinking"}:
        text = str(part.get("text") or part.get("content") or "").strip()
        if text:
            line = text.splitlines()[0].strip()
            if len(line) > 88:
                line = line[:85] + "..."
            return f"Planning: {line}"
        return ""
    if ptype in {"text", ""}:
        text = str(part.get("text") or "").strip()
        if not text:
            return ""
        line = text.splitlines()[0].strip()
        if len(line) > 88:
            line = line[:85] + "..."
        return f"Said: {line}"
    return ""


def concrete_actions_from_messages(messages: list[Any]) -> list[str]:
    """Ordered concrete tool/text actions from the whole session so far."""
    actions: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            phrase = _part_phrase(part)
            if not phrase or is_vague_activity(phrase):
                continue
            if phrase in seen:
                continue
            seen.add(phrase)
            actions.append(phrase)
    return actions


def activity_from_event(event: dict[str, Any]) -> str:
    """Best sentence from one OpenCode SSE payload."""
    etype = str(event.get("type") or event.get("event") or "")
    payload = event.get("properties") or event.get("payload") or event
    if not isinstance(payload, dict):
        return ""
    part = payload.get("part")
    if isinstance(part, dict):
        phrase = _part_phrase(part)
        if phrase:
            return phrase
    if payload.get("tool") or payload.get("name"):
        state = _state(payload.get("state")) or payload
        return describe_tool(str(payload.get("tool") or payload.get("name") or ""), state)
    if "file.edited" in etype or etype == "file.edited":
        label = short_path(payload.get("file") or payload.get("path"))
        return f"Changed {label}" if label else "Changed a file."
    return ""


def activity_from_messages(messages: list[Any]) -> str:
    """Best current sentence from the session transcript."""
    if not messages:
        return "Waiting for the model to start."
    actions = concrete_actions_from_messages(messages)
    if actions:
        return actions[-1]
    last = messages[-1]
    if not isinstance(last, dict):
        return "Waiting for the model’s next tool call."
    info = last.get("info") if isinstance(last.get("info"), dict) else {}
    parts = last.get("parts") if isinstance(last.get("parts"), list) else []
    completed = bool((info.get("time") or {}).get("completed")) if isinstance(info.get("time"), dict) else False
    if info.get("role") == "assistant" and not parts and not completed:
        return "Waiting for the model’s next tool call."
    return "Waiting for the model’s next tool call."


def assistant_text_from_messages(messages: list[Any]) -> str:
    """Concatenate assistant text parts from an OpenCode session transcript."""
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = str(info.get("role") or message.get("role") or "")
        if role != "assistant":
            continue
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in {None, "text"}:
                continue
            text = str(part.get("text") or "").strip()
            if text:
                chunks.append(text)
        direct = str(message.get("content") or message.get("text") or "").strip()
        if direct and direct not in chunks:
            chunks.append(direct)
    return "\n\n".join(chunks).strip()
