"""Per-project chat transcript for ordinary Q&A turns."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from local_coder.machine import config_dir

# A question and a change can finish at the same moment (each on its own thread): every read-modify-write of a
# transcript holds this lock, so neither turn is lost.
_WRITE_LOCK = threading.RLock()


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
    resolved = str(Path(project).expanduser().resolve())
    return {"path": resolved, "turns": _clean_turns(_read_json(chat_path(project)).get("turns"))}


def _read_json(path: Path) -> dict[str, Any]:
    """A saved transcript, or {} when it is missing or unreadable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_turns(turns: Any) -> list[dict[str, Any]]:
    """User and assistant turns with text, each with an id."""
    cleaned: list[dict[str, Any]] = []
    for row in turns if isinstance(turns, list) else []:
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
    return cleaned


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_chat(project: str | Path, turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist turns atomically."""
    repo = Path(project).expanduser().resolve()
    payload = {"path": str(repo), "turns": turns[-80:]}
    _write_json(chat_path(repo), payload)
    return payload


def append_turn(project: str | Path, role: str, text: str) -> dict[str, Any]:
    """Append one user or assistant turn and return the transcript."""
    if role not in {"user", "assistant"}:
        raise ValueError(f"unknown chat role {role!r}")
    body = text.strip()
    if not body:
        return load_chat(project)
    with _WRITE_LOCK:
        turns = list(load_chat(project)["turns"])
        turns.append({
            "id": f"turn-{int(time.time() * 1000)}-{len(turns)+1}",
            "role": role,
            "text": body,
            "ts": time.time(),
        })
        return save_chat(project, turns)


def archive_dir() -> Path:
    """Earlier conversations, kept when a new chat starts."""
    return chat_dir() / "archive"


def clear_chat(project: str | Path) -> dict[str, Any]:
    """Start a fresh transcript for this project; the old one is kept in archive/, never deleted."""
    with _WRITE_LOCK:
        return _clear_chat(project)


def _clear_chat(project: str | Path) -> dict[str, Any]:
    current = load_chat(project)
    if current["turns"]:
        folder = archive_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = folder / f"{chat_key(project)}-{stamp}.json"
        target.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
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


# Chats started without a project: one file each under chats/free/, listed in the sidebar by their first message.
_CHAT_ID = re.compile(r"^[A-Za-z0-9-]{6,64}$")


def free_chat_path(chat_id: str) -> Path:
    """The file of one chat without a project (ValueError for an id that is not one of ours)."""
    if not _CHAT_ID.fullmatch(chat_id or ""):
        raise ValueError(f"not a chat id: {chat_id!r}")
    return chat_dir() / "free" / f"{chat_id}.json"


def _chat_title(turns: list[dict[str, Any]]) -> str:
    first = next((turn["text"] for turn in turns if turn["role"] == "user"), "")
    words = " ".join(first.split())
    return words if len(words) <= 60 else words[:59].rstrip() + "…"


def load_free_chat(chat_id: str) -> dict[str, Any]:
    """A chat without a project: its id, title, last activity and turns (no turns when it was never saved)."""
    data = _read_json(free_chat_path(chat_id))
    turns = _clean_turns(data.get("turns"))
    return {"id": chat_id, "title": data.get("title") or _chat_title(turns), "updated": data.get("updated"),
            "turns": turns}


def append_free_turn(chat_id: str, role: str, text: str) -> dict[str, Any]:
    """Append one turn to a chat without a project and return it."""
    if role not in {"user", "assistant"}:
        raise ValueError(f"unknown chat role {role!r}")
    path = free_chat_path(chat_id)
    with _WRITE_LOCK:
        data = _read_json(path)
        turns = _clean_turns(data.get("turns"))
        body = text.strip()
        now = time.time()
        if body:
            turns.append({"id": f"turn-{int(now * 1000)}-{len(turns)+1}", "role": role, "text": body, "ts": now})
        payload = {
            "id": chat_id,
            "title": _chat_title(turns),
            "created": data.get("created") or now,
            "updated": now,
            "turns": turns[-80:],
        }
        _write_json(path, payload)
        return payload


def list_free_chats(limit: int = 30) -> list[dict[str, Any]]:
    """Saved chats without a project, the most recently active first."""
    folder = chat_dir() / "free"
    rows: list[dict[str, Any]] = []
    for path in folder.glob("*.json") if folder.is_dir() else []:
        data = _read_json(path)
        turns = _clean_turns(data.get("turns"))
        if not turns or not _CHAT_ID.fullmatch(path.stem):
            continue
        updated = data.get("updated")
        rows.append({
            "id": path.stem,
            "title": data.get("title") or _chat_title(turns),
            "updated": float(updated) if isinstance(updated, int | float) else path.stat().st_mtime,
        })
    rows.sort(key=lambda row: -row["updated"])
    return rows[:limit]


def recent_free_messages(chat_id: str, *, limit: int = 10, max_chars: int = 1500) -> list[dict[str, str]]:
    """The last turns of a chat without a project, clipped the way a project's are."""
    rows: list[dict[str, str]] = []
    for turn in load_free_chat(chat_id)["turns"][-limit:]:
        text = turn["text"]
        rows.append({"role": turn["role"], "content": text if len(text) <= max_chars else text[: max_chars - 1] + "…"})
    return rows


def move_free_chat(chat_id: str, project: str | Path) -> list[dict[str, Any]]:
    """Carry a chat without a project into the project it led to; the chat file goes to archive/, never deleted."""
    path = free_chat_path(chat_id)
    with _WRITE_LOCK:
        turns = load_free_chat(chat_id)["turns"]
        if not turns:
            return []
        save_chat(project, turns + load_chat(project)["turns"])
        folder = archive_dir()
        folder.mkdir(parents=True, exist_ok=True)
        os.replace(path, folder / f"free-{chat_id}-{time.strftime('%Y%m%d-%H%M%S')}.json")
        return turns
