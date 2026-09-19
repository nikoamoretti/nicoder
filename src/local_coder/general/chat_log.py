"""Per-project chat transcript for ordinary Q&A turns."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from local_coder.machine import config_dir


def chat_dir() -> Path:
    """Machine-local folder for chat transcripts (not inside the user project)."""
    path = config_dir() / "chats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chat_key(project: str | Path) -> str:
    """Stable id for a project path."""
    resolved = str(Path(project).expanduser().resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def chat_path(project: str | Path) -> Path:
    """JSON file for one project's chat turns."""
    return chat_dir() / f"{chat_key(project)}.json"


def load_chat(project: str | Path) -> dict[str, Any]:
    """Load the chat transcript for a project."""
    path = chat_path(project)
    empty = {
        "path": str(Path(project).expanduser().resolve()),
        "turns": [],
    }
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    turns = data.get("turns")
    if not isinstance(turns, list):
        turns = []
    cleaned = []
    for row in turns:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        text = str(row.get("text") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        cleaned.append({
            "id": str(row.get("id") or f"turn-{len(cleaned)+1}"),
            "role": role,
            "text": text,
            "ts": row.get("ts"),
        })
    return {"path": str(Path(project).expanduser().resolve()), "turns": cleaned}


def save_chat(project: str | Path, turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist turns atomically."""
    repo = Path(project).expanduser().resolve()
    payload = {"path": str(repo), "turns": turns[-80:]}
    path = chat_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return payload


def append_turn(project: str | Path, role: str, text: str) -> dict[str, Any]:
    """Append one user or assistant turn and return the transcript."""
    if role not in {"user", "assistant"}:
        raise ValueError(f"unknown chat role {role!r}")
    body = text.strip()
    if not body:
        return load_chat(project)
    data = load_chat(project)
    turns = list(data["turns"])
    turns.append({
        "id": f"turn-{int(time.time() * 1000)}-{len(turns)+1}",
        "role": role,
        "text": body,
        "ts": time.time(),
    })
    return save_chat(project, turns)


def clear_chat(project: str | Path) -> dict[str, Any]:
    """Forget the transcript for this project."""
    return save_chat(project, [])


def recent_messages(
    project: str | Path,
    *,
    limit: int = 8,
    max_chars: int = 500,
) -> list[dict[str, str]]:
    """Last session turns, clipped so a 4B context window can hold them."""
    rows: list[dict[str, str]] = []
    for turn in load_chat(project)["turns"][-limit:]:
        text = str(turn.get("text") or "").strip()
        role = str(turn.get("role") or "")
        if role not in {"user", "assistant"} or not text:
            continue
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        rows.append({"role": role, "content": text})
    return rows


def conversation_excerpt(
    project: str | Path,
    *,
    limit: int = 8,
    max_chars: int = 500,
) -> str:
    """Plain-text recent conversation for a coding prompt."""
    rows = recent_messages(project, limit=limit, max_chars=max_chars)
    if not rows:
        return ""
    lines = [f"{row['role']}: {row['content']}" for row in rows]
    return "Recent conversation:\n" + "\n".join(lines)
