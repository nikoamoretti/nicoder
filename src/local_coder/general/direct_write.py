"""Ask the local model for file bodies and apply them onto a snapshot.

Local Coder owns apply. The model cannot write the original tree. Keep/Discard
still decide what lands in the user's folder.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from local_coder.general.repair_context import RepairState, refresh_identity, repair_prompt_sections
from local_coder.general.snapshot import EXCLUDE
from local_coder.general.source_check import scan_text
from local_coder.machine import load_machine_config
from local_coder.server import ServerError, server_start

# Re-export for tests and callers that imported these from here.
__all__ = [
    "CompletionResult",
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
    "unwrap_completion",
]


@dataclass
class CompletionResult:
    """One model reply plus the metadata the apply path must honor."""

    text: str
    finish_reason: str | None = None
    truncated: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    request_identity: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


def unwrap_completion(value: str | CompletionResult) -> CompletionResult:
    """Accept a plain string from tests or a recorded completion."""
    if isinstance(value, CompletionResult):
        return value
    return CompletionResult(text=str(value or ""))


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
    repair: RepairState | None = None,
) -> str:
    """Ask the model for write, edit, or read blocks. No shell."""
    if repair is not None:
        return _repair_prompt(
            text, inspection, root, repair=repair,
            previous=previous, conversation=conversation, observations=observations,
        )
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


def _repair_prompt(
    text: str,
    inspection: dict[str, Any],
    root: Path,
    *,
    repair: RepairState,
    previous: str | None,
    conversation: str,
    observations: str,
) -> str:
    sample = repair.hits[0].rel if repair.hits else "src/App.tsx"
    seen = observations.strip()
    if seen and len(seen) > 2500:
        seen = seen[:2500] + "\n(read truncated; the diagnostic source above is complete)"
    seen_block = f"Additional files you asked to read:\n{seen}\n\n" if seen else ""
    convo = conversation.strip()
    if len(convo) > 1200:
        convo = "(earlier turns omitted)\n" + convo[-1000:]
    convo_block = f"{convo}\n\n" if convo else ""
    previous_block = ""
    if previous:
        previous_block = (
            "Previous model reply (proposal only; it was not applied):\n"
            f"{previous[:1200]}\n"
            "Reply with write, edit, or read fences only. Do not use tool_call XML.\n\n"
        )
    return (
        f"{repair_prompt_sections(repair)}"
        "Correct the current blocking diagnostic. "
        "Preserve existing behavior and unrelated source. "
        "Prefer a focused hash-edit on the failing location. "
        "A multi-file correction is allowed when the diagnostic requires it. "
        "Do not rewrite the application. Do not restyle the page. "
        "Do not claim the app is finished. Commentary is a proposal until checks run. "
        "Use only write, edit, or read blocks. No bash, no shell.\n"
        f"Write a full file:\n```{sample}\n…\n```\n"
        f"Or edit hashed lines:\n```edit {sample}\n-aaaaaaaa\n+  fixed line\n```\n"
        "Or read one extra file you were not shown:\n```read src/types.ts\n```\n\n"
        f"Project: {inspection.get('name')}\n"
        f"Existing files:\n{_tree_sketch(root)}\n\n"
        f"{seen_block}"
        f"{convo_block}"
        f"{previous_block}"
        f"Original user request (context, do not restart it):\n{repair.goal or text.strip()}\n"
    )


def local_complete(prompt: str) -> CompletionResult:
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
    prepared = time.perf_counter()
    policy = policy_for_runtime(n_ctx=int(cfg.n_ctx), output_reserve=max_tokens)
    requested_out, _out_source = resolve_output_limit(body, policy)
    policy = replace(policy, output_reserve=requested_out)
    counter = LlamaCppTokenizer(cfg.endpoint)
    measured = account(body, counter, policy)
    account_s = round(time.perf_counter() - prepared, 3)
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
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=cfg.gen_timeout, trust_env=False, follow_redirects=False) as client:
            response = client.post(url, json=payload)
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"The local model did not answer: {exc}") from exc
    elapsed_s = round(time.perf_counter() - started, 3)
    if response.status_code != 200:
        raise RuntimeError(f"The local model returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("The local model returned an unreadable reply.") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("The local model returned an empty reply.")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = str((message or {}).get("content") or choice.get("text") or "").strip()
    if not content:
        raise RuntimeError("The local model returned an empty reply.")
    finish = str(choice.get("finish_reason") or choice.get("native_finish_reason") or "")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    server_timing = data.get("timings") if isinstance(data.get("timings"), dict) else {}
    completion_tokens = usage.get("completion_tokens")
    truncated = finish.lower() in {"length", "max_tokens", "max_completion_tokens"}
    if isinstance(completion_tokens, int) and completion_tokens >= requested_out:
        truncated = True
    timing = {
        "account_s": account_s,
        "generation_s": elapsed_s,
        "prompt_ms": server_timing.get("prompt_ms"),
        "predicted_ms": server_timing.get("predicted_ms"),
        "predicted_per_second": server_timing.get("predicted_per_second"),
    }
    return CompletionResult(
        text=content,
        finish_reason=finish or None,
        truncated=truncated,
        usage={k: usage[k] for k in usage if k in {"prompt_tokens", "completion_tokens", "total_tokens"}},
        timing={key: value for key, value in timing.items() if value is not None},
        request_identity={
            "id": data.get("id"),
            "model": data.get("model") or cfg.model_alias,
            "max_tokens": requested_out,
            "n_ctx": int(cfg.n_ctx),
        },
        raw={"id": data.get("id"), "finish_reason": finish, "usage": usage, "timings": server_timing},
    )


def complete_files(
    root: Path,
    text: str,
    inspection: dict[str, Any],
    *,
    complete: Callable[[str], str | CompletionResult] | None = None,
    previous: str | None = None,
    conversation: str = "",
    retry_reason: str | None = None,
    repair: RepairState | None = None,
    on_event: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[str], str, list[str]]:
    """Ask the model. Apply write/edit only when the preview can compile."""
    generate = complete or local_complete
    observations = ""
    last_raw = ""
    last_problems: list[str] = []
    focus = {hit.rel: int(hit.line) for hit in (repair.hits if repair else []) if hit.line}
    for turn in range(2):
        if cancel is not None and cancel.is_set():
            return [], last_raw, ["cancelled"]
        if repair is not None:
            repair = refresh_identity(repair, root)
        prompt = implement_prompt(
            text, inspection, root, previous=previous, conversation=conversation,
            retry_reason=retry_reason, observations=observations, repair=repair,
        )
        result = unwrap_completion(generate(prompt))
        last_raw = result.text
        if on_event:
            on_event("completion", _completion_note(result))
        if cancel is not None and cancel.is_set():
            return [], result.text, ["cancelled"]
        if result.truncated:
            return [], result.text, [
                "truncated: the model reply was cut off at the output limit and was not applied",
            ]
        parsed = parse_actions(result.text)
        if parsed.writes or parsed.edits:
            files, problems = materialize(root, parsed)
            if problems or not files:
                return [], result.text, problems
            if cancel is not None and cancel.is_set():
                return [], result.text, ["cancelled"]
            written = apply_files(root, files)
            if on_event:
                for rel in written:
                    on_event("edit" if rel not in parsed.writes else "write", rel)
            return written, result.text, []
        if parsed.reads and turn == 0:
            if on_event:
                for rel in parsed.reads:
                    on_event("read", rel)
            observations = read_files(root, parsed.reads, around=focus)
            if observations:
                continue
        last_problems = list(parsed.refused)
        break
    return [], last_raw, last_problems


def _completion_note(result: CompletionResult) -> str:
    finish = result.finish_reason or "unknown"
    identity = result.request_identity or {}
    usage = result.usage or {}
    timing = result.timing or {}
    parts = [
        f"finish_reason={finish}",
        f"truncated={result.truncated}",
        f"id={identity.get('id') or ''}",
        f"model={identity.get('model') or ''}",
        f"max_tokens={identity.get('max_tokens') or ''}",
        f"n_ctx={identity.get('n_ctx') or ''}",
        f"prompt_tokens={_recorded(usage.get('prompt_tokens'))}",
        f"completion_tokens={_recorded(usage.get('completion_tokens'))}",
        f"account_s={_recorded(timing.get('account_s'))}",
        f"generation_s={_recorded(timing.get('generation_s'))}",
        f"prompt_ms={_recorded(timing.get('prompt_ms'))}",
        f"predicted_ms={_recorded(timing.get('predicted_ms'))}",
    ]
    return " ".join(parts)


def _recorded(value: Any) -> str:
    return "unavailable" if value is None else str(value)


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
