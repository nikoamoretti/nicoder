"""Files and images attached to a message: what the model is told, and the images it is shown.

The Mac app copies each attachment into `<data root>/attachments/<id>/` (the agent's tools can read it there), and
beside it the text macOS found in it (a PDF's text, the words in an image) and, for an image, a smaller copy for the
model. Nico, 2026-10-26: "i need nicoder to be able to accept files and images and be able to process them".
"""
from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_coder.engines.opencode_config import data_root

KINDS = ("image", "pdf", "file")
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # a shown image is sent whole; the app's smaller copy keeps it well under this


@dataclass(frozen=True)
class Attachment:
    """One attached file, as the app staged it."""

    path: Path
    name: str
    kind: str
    text: Path | None = None
    preview: Path | None = None


def attachments_dir() -> Path:
    """Where the app stages attachments: inside Nicoder's data folder, readable by the agent's tools."""
    return data_root() / "attachments"


def _staged(raw: Any) -> Path | None:
    """A path the app staged (a file inside the attachments folder), or None."""
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    try:
        resolved = path.resolve(strict=True)
        root = attachments_dir().resolve()
    except OSError:
        return None
    if resolved.is_file() and root in resolved.parents:
        return resolved
    return None


def parse(raw: Any) -> list[Attachment]:
    """The attachments of one message; anything that is not a staged file is left out."""
    found: list[Attachment] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        path = _staged(row.get("path"))
        if path is None:
            continue
        kind = str(row.get("kind") or "file")
        found.append(Attachment(
            path=path,
            name=str(row.get("name") or path.name).strip() or path.name,
            kind=kind if kind in KINDS else "file",
            text=_staged(row.get("text")),
            preview=_staged(row.get("preview")),
        ))
    return found


def sees_images(model_id: str, provider: str) -> bool:
    """Whether the selected model can be shown images (OrcaRouter's catalog says so per model)."""
    if provider != "orcarouter":
        return False
    from local_coder.orcarouter import find_model

    row = find_model(model_id)
    return bool(row and row.get("vision"))


def note(attachments: list[Attachment], *, shown: bool) -> str:
    """The lines the model reads about the attachments (appended to the user's message)."""
    if not attachments:
        return ""
    lines = ["Attached files (read them with your tools):"]
    for item in attachments:
        if item.kind == "image":
            seen = "shown to you with this message" if shown else "this model cannot see images"
            text = f"; the words macOS read in it: {item.text}" if item.text else ""
            lines.append(f"- {item.name}: an image at {item.path} ({seen}){text}")
        elif item.kind == "pdf":
            text = f"; its text: {item.text}" if item.text else " (no text could be taken from it)"
            lines.append(f"- {item.name}: a PDF at {item.path}{text}")
        else:
            lines.append(f"- {item.name}: {item.path}")
    return "\n".join(lines)


def image_urls(attachments: list[Attachment]) -> list[str]:
    """Data URLs of the images to show the model (the smaller copy when there is one), at most MAX_IMAGES."""
    urls: list[str] = []
    for item in attachments:
        if item.kind != "image" or len(urls) >= MAX_IMAGES:
            continue
        source = item.preview or item.path
        try:
            data = source.read_bytes()
        except OSError:
            continue
        if not data or len(data) > MAX_IMAGE_BYTES:
            continue
        mime = mimetypes.guess_type(source.name)[0] or "image/png"
        urls.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
    return urls


def turn_text(text: str, attachments: list[Attachment]) -> str:
    """The user's turn as the chat keeps it: the message, then the attached names."""
    body = text.strip()
    if not attachments:
        return body
    names = ", ".join(item.name for item in attachments)
    return f"{body}\n\nattached: {names}" if body else f"attached: {names}"
