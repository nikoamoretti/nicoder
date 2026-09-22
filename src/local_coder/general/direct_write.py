"""Ask the local model for file bodies and apply them onto a snapshot.

Local Coder owns apply. The model cannot write the original tree. Keep/Discard
still decide what lands in the user's folder.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from local_coder.general.harness import (
    format_hashed,
    header_kind,
    iter_fences,
    materialize,
    parse_actions,
    path_from_header,
    read_files,
    safe_rel,
    strip_think,
)
from local_coder.general.snapshot import EXCLUDE
from local_coder.general.source_check import scan_text
from local_coder.machine import load_machine_config
from local_coder.server import ServerError, server_start

# Re-export for tests and callers that imported these from here.
__all__ = [
    "apply_files",
    "checked_blocks",
    "complete_files",
    "fallback_explanation",
    "implement_files",
    "implement_prompt",
    "is_blank_starter",
    "local_complete",
    "parse_file_blocks",
    "reply_prose",
    "safe_rel",
]


def is_blank_starter(root: Path) -> bool:
    """True when this tree is still the empty Vite starter."""
    if not (root / "package.json").is_file():
        return False
    app = root / "src" / "App.tsx"
    if not app.is_file():
        return True
    try:
        text = app.read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "empty on purpose" in text or "<h1>new app</h1>" in text


def parse_file_blocks(text: str) -> dict[str, str]:
    """Extract path → body from write fences. Read/edit/shell blocks are ignored."""
    files: dict[str, str] = {}
    for match in iter_fences(strip_think(text or "")):
        header = match.group("header") or ""
        kind, rel = header_kind(header)
        if kind != "write":
            continue
        if not rel:
            rel = path_from_header(header)
        if not rel:
            continue
        body = match.group("body")
        if not body.endswith("\n"):
            body += "\n"
        files[rel] = body
    return files


def reply_prose(text: str) -> str:
    """Plain-language text before the first file fence."""
    cleaned = strip_think(text or "").strip()
    before = cleaned.split("```", 1)[0].strip()
    if len(before) < 24:
        return ""
    return before[:1200]


def fallback_explanation(written: list[str]) -> str:
    """Honest fallback when the model wrote files but no prose."""
    if not written:
        return ""
    if len(written) == 1:
        return (
            f"I updated {written[0]}. Your original folder is unchanged until you Keep."
        )
    return (
        f"I updated {len(written)} files ({', '.join(written)}). "
        "Your original folder is unchanged until you Keep."
    )


def checked_blocks(raw: str) -> tuple[dict[str, str], list[str]]:
    """Parse write fences and list problems Vite cannot compile."""
    files = parse_file_blocks(raw)
    problems: list[str] = []
    for rel, body in files.items():
        problems.extend(scan_text(rel, body))
    return files, problems


def apply_files(root: Path, files: dict[str, str]) -> list[str]:
    """Write parsed files under root. Returns relative paths that were written."""
    written: list[str] = []
    for rel, body in files.items():
        safe = safe_rel(rel)
        if not safe:
            continue
        dest = (root / safe).resolve()
        try:
            dest.relative_to(root.resolve())
        except ValueError:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written.append(safe)
    return written


def _tree_sketch(root: Path) -> str:
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in EXCLUDE for part in path.relative_to(root).parts):
            continue
        if len(lines) >= 24:
            lines.append("…")
            break
        lines.append(rel)
    return "\n".join(lines) or "(empty)"


def implement_prompt(
    text: str,
    inspection: dict[str, Any],
    root: Path,
    *,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
    observations: str = "",
) -> str:
    """Ask the model for write, edit, or read blocks. No shell."""
    app = ""
    app_path = root / "src" / "App.tsx"
    if app_path.is_file():
        try:
            app = format_hashed(app_path.read_text(encoding="utf-8"))
        except OSError:
            app = ""
    retry = ""
    if retry_reason:
        retry = retry_reason.strip() + "\n"
        if previous:
            retry += f"Previous reply:\n{previous[:2000]}\n\n"
    elif previous:
        retry = (
            "Your last reply had no usable file blocks. "
            "Reply again with fenced files only.\n"
            f"Previous reply:\n{previous[:2000]}\n\n"
        )
    convo_raw = conversation.strip()
    if len(convo_raw) > 2500:
        convo_raw = "(earlier turns omitted)\n" + convo_raw[-2200:]
    convo = f"{convo_raw}\n\n" if convo_raw else ""
    seen_raw = observations.strip()
    if len(seen_raw) > 3000:
        seen_raw = seen_raw[:3000] + "\n(observation truncated; use another read for the rest)"
    seen = f"Files you asked to read:\n{seen_raw}\n\n" if seen_raw else ""
    return (
        f"{retry}"
        "Implement the user's request now. "
        "Do not plan. Do not say the work must happen outside Local Coder. "
        "First write 2-4 sentences for the user: what changed and how the app "
        "looks or behaves now. No code, no file-path dump. "
        "Then use only these blocks. No bash, no shell, no other tools.\n"
        "Write a full file:\n"
        "```src/App.tsx\n"
        "export default function App() {\n"
        "  return <main>…</main>\n"
        "}\n"
        "```\n"
        "Or edit hashed lines (the 8 characters before |):\n"
        "```edit src/App.tsx\n"
        "-aaaaaaaa\n"
        "+  return <h1>Board</h1>\n"
        "```\n"
        "Or read one file you were not shown:\n"
        "```read src/App.css\n"
        "```\n"
        "You may create or replace files under src/, electron/, public/, "
        "plus root files such as index.html or package.json. "
        "For a web UI, also write src/App.css. Make it look finished: a real page "
        "background, a type scale, spacing, and cards or columns. Do not ship a "
        "row of default pills on a blank page. "
        "Write real TypeScript when the project is TypeScript. Generics, typed "
        "useState, and Record<...> are fine. Use straight quotes, not curly quotes.\n\n"
        f"Project: {inspection.get('name')}\n"
        f"Languages: {', '.join(inspection.get('languages') or []) or 'TypeScript'}\n"
        f"Existing files:\n{_tree_sketch(root)}\n\n"
        f"Current src/App.tsx (hash|line):\n{app}\n\n"
        f"{seen}"
        f"{convo}"
        f"User request:\n{text.strip()}\n"
    )


def local_complete(prompt: str) -> str:
    """One non-streaming chat completion from the owned llama.cpp server."""
    from dataclasses import replace

    from local_coder.engines.context_budget import (
        LlamaCppTokenizer,
        account,
        policy_for_runtime,
        resolve_output_limit,
    )

    cfg = load_machine_config()
    try:
        server_start(cfg, wait_s=300)
    except ServerError:
        pass
    system = (
        "You change files in Local Coder with write, edit, or read blocks. "
        "First a short plain-language summary. No plans. No shell."
    )
    max_tokens = max(256, int(cfg.max_tokens or 2048))
    body: dict[str, Any] = {
        "model": cfg.model_alias,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "max_completion_tokens": max_tokens,
    }
    policy = policy_for_runtime(n_ctx=int(cfg.n_ctx), output_reserve=max_tokens)
    requested_out, _out_source = resolve_output_limit(body, policy)
    policy = replace(policy, output_reserve=requested_out)
    counter = LlamaCppTokenizer(cfg.endpoint)
    measured = account(body, counter, policy)
    if not measured.allowed:
        label = "estimated" if measured.estimated or counter.labeled == "estimate" else "counted"
        raise RuntimeError(
            f"This request does not fit the local model context ({label}: "
            f"input {measured.input_tokens} + output {policy.output_reserve} + "
            f"margin {policy.safety_margin} vs n_ctx {policy.n_ctx}). "
            "The context window was not increased."
        )
    url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": cfg.model_alias,
        "messages": body["messages"],
        "temperature": 0.2,
        "max_tokens": requested_out,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=cfg.gen_timeout, trust_env=False, follow_redirects=False) as client:
            response = client.post(url, json=payload)
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"The local model did not answer: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"The local model returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("The local model returned an unreadable reply.") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("The local model returned an empty reply.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "").strip()
    if not content and isinstance(choices[0], dict):
        content = str(choices[0].get("text") or "").strip()
    if not content:
        raise RuntimeError("The local model returned an empty reply.")
    return content


def complete_files(
    root: Path,
    text: str,
    inspection: dict[str, Any],
    *,
    complete: Callable[[str], str] | None = None,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
    on_event: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[str], str, list[str]]:
    """Ask the model. Apply write/edit only when the preview can compile."""
    generate = complete or local_complete
    observations = ""
    last_raw = ""
    last_problems: list[str] = []
    for turn in range(2):
        if cancel is not None and cancel.is_set():
            return [], last_raw, ["cancelled"]
        prompt = implement_prompt(
            text, inspection, root, previous=previous, conversation=conversation,
            retry_reason=retry_reason, observations=observations,
        )
        raw = generate(prompt)
        last_raw = raw
        if cancel is not None and cancel.is_set():
            return [], raw, ["cancelled"]
        parsed = parse_actions(raw)
        if parsed.writes or parsed.edits:
            files, problems = materialize(root, parsed)
            if problems or not files:
                return [], raw, problems
            if cancel is not None and cancel.is_set():
                return [], raw, ["cancelled"]
            written = apply_files(root, files)
            if on_event:
                for rel in written:
                    on_event("edit" if rel not in parsed.writes else "write", rel)
            return written, raw, []
        if parsed.reads and turn == 0:
            if on_event:
                for rel in parsed.reads:
                    on_event("read", rel)
            observations = read_files(root, parsed.reads)
            if observations:
                previous = None
                retry_reason = None
                continue
        last_problems = list(parsed.refused)
        break
    return [], last_raw, last_problems


def implement_files(
    root: Path,
    text: str,
    inspection: dict[str, Any],
    *,
    complete: Callable[[str], str] | None = None,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
) -> list[str]:
    """Ask the model for files and write them under root."""
    written, _raw, _problems = complete_files(
        root, text, inspection, complete=complete, previous=previous,
        conversation=conversation, retry_reason=retry_reason,
    )
    return written
