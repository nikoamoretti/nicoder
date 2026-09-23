"""Ask the local model for file bodies and apply them onto a snapshot.

Local Coder owns apply. The model cannot write the original tree. Keep/Discard
still decide what lands in the user's folder.
"""
from __future__ import annotations

import json
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
from local_coder.general.repair_context import (
    FileView,
    RepairState,
    note_shown,
    refresh_identity,
    render_repair_sections,
    serve_read,
)
from local_coder.general.snapshot import EXCLUDE, iter_source_files
from local_coder.general.source_check import scan_text
from local_coder.machine import load_machine_config
from local_coder.server import ServerError, server_start

# Re-export for tests and callers that imported these from here.
__all__ = [
    "BudgetExhausted",
    "CompletionResult",
    "PromptTooLarge",
    "ReplyOutcome",
    "apply_files",
    "build_repair_prompt",
    "checked_blocks",
    "complete_files",
    "fallback_explanation",
    "implement_files",
    "implement_prompt",
    "is_blank_starter",
    "local_complete",
    "parse_file_blocks",
    "prompt_budget_chars",
    "reply_prose",
    "safe_rel",
    "unwrap_completion",
]

# Measured 2.64 chars/token on hashed TypeScript repair prompts with the Bonsai tokenizer.
_CHARS_PER_TOKEN = 2.4
_SYSTEM_TOKENS = 60
_REPAIR_TURNS = 3


class PromptTooLarge(RuntimeError):
    """The assembled prompt does not fit the served context. Nothing was generated."""


class BudgetExhausted(RuntimeError):
    """The request's generation or wall budget does not allow another model call."""


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


@dataclass
class ReplyOutcome:
    """What Local Coder did with one model reply. Runtime facts, not the model's claims."""

    kind: str
    files: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    completion: dict[str, Any] = field(default_factory=dict)
    already_shown: list[str] = field(default_factory=list)

    def status(self) -> str:
        """One plain sentence for the next prompt. Says 'not applied' only when that is true."""
        problems = "; ".join(self.problems)[:600]
        if self.kind == "applied":
            return (
                f"Your previous reply was applied: it changed {', '.join(self.files)}. "
                "Local Coder reran the checks; the diagnostic below is the result after that change."
            )
        if self.kind == "read":
            seen = ""
            if self.already_shown:
                seen = f" Already shown in full, so not repeated: {', '.join(self.already_shown)}."
            refused = f" Not read: {problems}." if self.problems else ""
            return (
                f"You asked to read {', '.join(self.reads)}. Local Coder read them; the lines are in "
                f"the Source section below.{seen}{refused} Nothing was changed yet."
            )
        if self.kind == "repeat_read":
            return (
                "Your previous reply asked for lines you were already shown, so nothing new was read "
                "and nothing was changed. Make a focused edit now."
            )
        if self.kind == "rejected":
            return f"Your previous reply was not applied, so the candidate did not change: {problems}"
        if self.kind == "truncated":
            return (
                "Your previous reply was cut off at the output limit and was not applied. "
                "Nothing was changed. Send a smaller, focused edit."
            )
        if self.kind == "no_action":
            return (
                "Your previous reply had no edit, write, or read block that Local Coder could run, "
                "so nothing was read or applied. To read files, send one read fence per file exactly "
                "as in the Blocks section below; to change code, use edit blocks."
            )
        if self.kind == "error":
            return f"The previous model call failed before any reply was applied: {problems}"
        return ""


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
    views: dict[str, FileView] | None = None,
    previous_outcome: ReplyOutcome | None = None,
) -> str:
    """Ask the model for write, edit, or read blocks. No shell."""
    if repair is not None:
        prompt, _shown = build_repair_prompt(
            text, inspection, root, repair=repair, views=views,
            previous_outcome=previous_outcome, previous=previous,
        )
        return prompt
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


def prompt_budget_chars() -> int:
    """Largest repair prompt, in characters, that fits the served context after output reserve."""
    from local_coder.engines.context_budget import policy_for_runtime

    cfg = load_machine_config()
    policy = policy_for_runtime(n_ctx=int(cfg.n_ctx), output_reserve=max(256, int(cfg.max_tokens or 2048)))
    tokens = (
        policy.max_input_tokens()
        - policy.template_overhead
        - 2 * policy.per_message_overhead
        - _SYSTEM_TOKENS
    )
    return max(2400, int(tokens * _CHARS_PER_TOKEN))


def build_repair_prompt(
    text: str,
    inspection: dict[str, Any],
    root: Path,
    *,
    repair: RepairState,
    views: dict[str, FileView] | None = None,
    previous_outcome: ReplyOutcome | None = None,
    previous: str | None = None,
    budget_scale: float = 1.0,
) -> tuple[str, dict[str, set[int]]]:
    """Repair prompt within the context budget. Returns the prompt and the source lines it shows."""
    sample = repair.hits[0].rel if repair.hits else "src/App.tsx"
    previous_block = ""
    if previous and previous_outcome is not None and previous_outcome.kind in {"rejected", "no_action"}:
        previous_block = f"Previous reply (not applied):\n{previous.strip()[:500]}\n\n"
    instructions = (
        "Fix the current blocking diagnostic. Keep existing behavior and unrelated source. "
        "Prefer focused hash-edits: one edit block per consecutive span, and several blocks or "
        "files when the diagnostic needs them. Do not rewrite the application, restyle the page, "
        "or remove a feature to silence an error. Your commentary is a proposal until checks run.\n"
        "Blocks (no bash, no shell; tool_call XML only works for reads):\n"
        f"```edit {sample}\n-a1b2c3d4\n+replacement line\n```\n"
        "- lines give the hash of each current line to replace, in order; + lines are the new text. "
        "To insert, repeat an existing line as both - and +.\n"
        "```read src/types.ts\n```\n"
        "shows more of a file; read the same file again for its next part.\n"
        f"```{sample}\nfull file text\n```\n"
        "replaces a whole file.\n\n"
    )
    tail = (
        f"Project: {inspection.get('name')}\n"
        f"Files: {_file_list(root)}\n\n"
        f"{previous_block}"
        "Original user request (context; do not restart it):\n"
        f"{repair.goal or text.strip()}\n"
    )
    budget = int(prompt_budget_chars() * budget_scale)
    status = previous_outcome.status() if previous_outcome is not None else ""
    sections, shown = render_repair_sections(
        repair, root, views, budget=max(1200, budget - len(instructions) - len(tail)), status=status,
    )
    return sections + "\n" + instructions + tail, shown


def _file_list(root: Path) -> str:
    names = sorted(path.relative_to(root).as_posix() for path in iter_source_files(root))
    if len(names) > 40:
        names = names[:40] + ["…"]
    return ", ".join(names) or "(empty)"


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
        raise PromptTooLarge(
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
        "account_input_tokens": measured.input_tokens,
        "account_counter": counter.labeled,
        "generation_s": elapsed_s,
        **{
            key: server_timing.get(key)
            for key in (
                "cache_n",
                "prompt_n",
                "prompt_ms",
                "prompt_per_second",
                "predicted_n",
                "predicted_ms",
                "predicted_per_second",
            )
        },
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


_TRUNCATED = "truncated: the model reply was cut off at the output limit and was not applied"


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
    outcomes: list[ReplyOutcome] | None = None,
    views: dict[str, FileView] | None = None,
    previous_outcome: ReplyOutcome | None = None,
) -> tuple[list[str], str, list[str]]:
    """Ask the model. Apply write/edit only when the preview can compile. Reads are intermediate steps."""
    generate = complete or local_complete
    record = outcomes if outcomes is not None else []
    if repair is not None:
        return _complete_repair(
            root, text, inspection, generate, repair=repair, previous=previous, on_event=on_event,
            cancel=cancel, record=record, views=views if views is not None else {}, status=previous_outcome,
        )
    observations = ""
    last_raw = ""
    last_problems: list[str] = []
    for turn in range(2):
        if cancel is not None and cancel.is_set():
            record.append(ReplyOutcome("cancelled"))
            return [], last_raw, ["cancelled"]
        prompt = implement_prompt(
            text, inspection, root, previous=previous, conversation=conversation,
            retry_reason=retry_reason, observations=observations,
        )
        try:
            result = unwrap_completion(generate(prompt))
        except BudgetExhausted as exc:
            record.append(ReplyOutcome("budget", problems=[str(exc)]))
            return [], last_raw, []
        last_raw = result.text
        completion = _completion_record(result)
        _report(on_event, prompt, result)
        if cancel is not None and cancel.is_set():
            record.append(ReplyOutcome("cancelled", completion=completion))
            return [], result.text, ["cancelled"]
        if result.truncated:
            record.append(ReplyOutcome("truncated", problems=[_TRUNCATED], completion=completion))
            return [], result.text, [_TRUNCATED]
        parsed = parse_actions(result.text)
        if parsed.writes or parsed.edits:
            files, problems = materialize(root, parsed)
            if problems or not files:
                record.append(ReplyOutcome("rejected", problems=problems, completion=completion))
                return [], result.text, problems
            if cancel is not None and cancel.is_set():
                record.append(ReplyOutcome("cancelled", completion=completion))
                return [], result.text, ["cancelled"]
            written = apply_files(root, files)
            record.append(ReplyOutcome("applied", files=written, completion=completion))
            if on_event:
                for rel in written:
                    on_event("edit" if rel not in parsed.writes else "write", rel)
            return written, result.text, []
        if parsed.reads and turn == 0:
            if on_event:
                for rel in parsed.reads:
                    on_event("read", rel)
            observations = read_files(root, parsed.reads)
            if observations:
                record.append(ReplyOutcome("read", reads=list(parsed.reads), completion=completion))
                continue
        last_problems = list(parsed.refused)
        record.append(ReplyOutcome("no_action", problems=last_problems, completion=completion))
        break
    return [], last_raw, last_problems


def _complete_repair(
    root: Path,
    text: str,
    inspection: dict[str, Any],
    generate: Callable[[str], str | CompletionResult],
    *,
    repair: RepairState,
    previous: str | None,
    on_event: Callable[[str, str], None] | None,
    cancel: threading.Event | None,
    record: list[ReplyOutcome],
    views: dict[str, FileView],
    status: ReplyOutcome | None,
) -> tuple[list[str], str, list[str]]:
    """Repair turns: a read is served and the model is asked again; an edit ends the call."""
    last_raw = ""
    scale = 1.0
    turns = 0
    while turns < _REPAIR_TURNS:
        if cancel is not None and cancel.is_set():
            record.append(ReplyOutcome("cancelled"))
            return [], last_raw, ["cancelled"]
        repair = refresh_identity(repair, root)
        prompt, shown = build_repair_prompt(
            text, inspection, root, repair=repair, views=views,
            previous_outcome=status, previous=previous, budget_scale=scale,
        )
        try:
            result = unwrap_completion(generate(prompt))
        except PromptTooLarge as exc:
            if scale <= 0.5:
                record.append(ReplyOutcome("error", problems=[str(exc)]))
                return [], last_raw, [str(exc)]
            scale = round(scale * 0.8, 3)
            continue
        except BudgetExhausted as exc:
            record.append(ReplyOutcome("budget", problems=[str(exc)]))
            return [], last_raw, []
        turns += 1
        note_shown(views, root, shown)
        last_raw = result.text
        completion = _completion_record(result)
        _report(on_event, prompt, result)
        if cancel is not None and cancel.is_set():
            record.append(ReplyOutcome("cancelled", completion=completion))
            return [], result.text, ["cancelled"]
        if result.truncated:
            record.append(ReplyOutcome("truncated", problems=[_TRUNCATED], completion=completion))
            return [], result.text, [_TRUNCATED]
        parsed = parse_actions(result.text)
        if parsed.writes or parsed.edits:
            files, problems = materialize(root, parsed)
            problems += _build_script_problems(root, files)
            files = {rel: body for rel, body in files.items() if _differs(root, rel, body)}
            if problems or not files:
                problems = problems or ["the reply produced the same file contents, so nothing changed"]
                record.append(ReplyOutcome("rejected", problems=problems, completion=completion))
                return [], result.text, problems
            if cancel is not None and cancel.is_set():
                record.append(ReplyOutcome("cancelled", completion=completion))
                return [], result.text, ["cancelled"]
            written = apply_files(root, files)
            record.append(ReplyOutcome("applied", files=written, completion=completion))
            if on_event:
                for rel in written:
                    on_event("edit" if rel not in parsed.writes else "write", rel)
            return written, result.text, []
        if parsed.reads:
            served: list[str] = []
            seen: list[str] = []
            refused: list[str] = []
            for rel in parsed.reads:
                state, count = serve_read(views, root, rel)
                if on_event:
                    on_event("read", f"{rel} ({state}, {count} new lines)")
                if state == "read" and count:
                    served.append(rel)
                elif state == "seen":
                    seen.append(rel)
                else:
                    refused.append(f"{rel}: {_READ_REFUSAL[state]}")
            if served:
                status = ReplyOutcome(
                    "read", reads=list(parsed.reads), problems=refused, completion=completion, already_shown=seen,
                )
                record.append(status)
                previous = None
                continue
            kind = "rejected" if refused else "repeat_read"
            record.append(ReplyOutcome(kind, reads=list(parsed.reads), problems=refused, completion=completion))
            return [], result.text, refused
        record.append(ReplyOutcome("no_action", problems=list(parsed.refused), completion=completion))
        return [], result.text, list(parsed.refused)
    return [], last_raw, []


_READ_REFUSAL = {
    "refused": "refused, the path resolves outside the candidate",
    "missing": "file is missing from the candidate",
    "unreadable": "file could not be read",
}


def _differs(root: Path, rel: str, body: str) -> bool:
    try:
        return (root / rel).read_text(encoding="utf-8") != body
    except (OSError, UnicodeDecodeError):
        return True


def _build_script_problems(root: Path, files: dict[str, str]) -> list[str]:
    """During repair the build script is the check itself, so the model cannot change it."""
    body = files.get("package.json")
    if body is None:
        return []
    try:
        new = json.loads(body)
    except ValueError:
        return ["package.json: the new file is not valid JSON"]
    try:
        old = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    old_build = (old.get("scripts") or {}).get("build") if isinstance(old, dict) else None
    new_build = (new.get("scripts") or {}).get("build") if isinstance(new, dict) else None
    if old_build != new_build:
        return ["package.json: the build script is what checks this repair, so it cannot change during repair"]
    return []


def _report(on_event: Callable[[str, str], None] | None, prompt: str, result: CompletionResult) -> None:
    if not on_event:
        return
    on_event("prompt", prompt)
    on_event("completion", _completion_note(result))
    on_event("reply", result.text)


def _completion_record(result: CompletionResult) -> dict[str, Any]:
    return {
        "finish_reason": result.finish_reason,
        "truncated": result.truncated,
        "usage": dict(result.usage or {}),
        "timing": dict(result.timing or {}),
        "request_identity": dict(result.request_identity or {}),
    }


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
        f"cache_n={_recorded(timing.get('cache_n'))}",
        f"prompt_n={_recorded(timing.get('prompt_n'))}",
        f"prompt_ms={_recorded(timing.get('prompt_ms'))}",
        f"prompt_per_second={_recorded(timing.get('prompt_per_second'))}",
        f"predicted_ms={_recorded(timing.get('predicted_ms'))}",
        f"predicted_per_second={_recorded(timing.get('predicted_per_second'))}",
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
