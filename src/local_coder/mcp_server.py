"""MCP server that lets Claude Code (or any MCP client) delegate work to the cheaper hosted model.

Tools: nemotron_explore, nemotron_draft_change, nemotron_apply_draft, nemotron_discard_draft.
Transport: stdio, one JSON-RPC 2.0 message per line, standard library only. Each
tool call runs on its own thread so a client can run several; writes to stdout
are serialized. Progress goes out as notifications/progress when the client
sends a progressToken; notifications/cancelled stops the matching call.

Run: python -m local_coder.mcp_server
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any, TextIO

from . import __version__
from .agent import delegate

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "local-coder"
INSTRUCTIONS = (
    "Cost saver: these tools hand work to NVIDIA's Nemotron 3.5 Super 120B (free, slower and less reliable than "
    "Claude) through Local Coder's sandboxed agent, so Claude spends far fewer tokens. Use nemotron_explore for "
    "reading-heavy questions (summarize a repo or module, find where something is implemented, trace a flow, scan "
    "large files) instead of reading many files yourself, then spot-check its key claims in one or two of the "
    "files it cites. Use nemotron_draft_change for well-specified, mechanical edits (give exact instructions and "
    "file names); review the returned diff, then call nemotron_apply_draft or nemotron_discard_draft. Keep "
    "planning, judgment, subtle debugging and final review on Claude. Calls take from 30 seconds to several "
    "minutes. Whatever Nemotron reads is sent to NVIDIA; secrets and ~/Library are always blocked."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "nemotron_explore",
        "description": (
            "Delegate a code-reading question to Nemotron 3.5 Super (free). It searches and reads files itself "
            "(read-only, secrets blocked) and returns a short answer with path:line evidence plus the list of "
            "files it read. Good for: summarizing a codebase or module, finding where something lives, tracing "
            "how data flows, checking how many places use something, digesting large files. Verify key claims "
            "before acting on them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, with any context the reader needs."},
                "project_path": {
                    "type": "string",
                    "description": "Absolute path of the project to look in (reads stay inside it). Omit to let it "
                    "search the home folder.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "nemotron_draft_change",
        "description": (
            "Delegate a well-specified code change to Nemotron 3.5 Super (free). It edits a private copy of the "
            "project, can run installs, builds and tests in a sandbox, and returns a diff, a short summary and "
            "a draft_id. Nothing changes in the project until nemotron_apply_draft. Best for mechanical or "
            "clearly specified work: exact instructions, named files, expected behavior."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "Absolute path of the project to change."},
                "task": {"type": "string", "description": "Exactly what to change, where, and how to check it."},
            },
            "required": ["project_path", "task"],
        },
    },
    {
        "name": "nemotron_apply_draft",
        "description": (
            "Apply a reviewed draft to its project. Refuses, changing nothing, if the same files changed in the "
            "project since the draft began."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string", "description": "The draft_id returned by the draft."}},
            "required": ["draft_id"],
        },
    },
    {
        "name": "nemotron_discard_draft",
        "description": "Throw a draft away. The project was never touched.",
        "inputSchema": {
            "type": "object",
            "properties": {"draft_id": {"type": "string", "description": "The draft_id to discard."}},
            "required": ["draft_id"],
        },
    },
]


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def format_explore(result: dict[str, Any]) -> str:
    """The explore answer plus a compact footer the caller can verify against."""
    files = result.get("files_read") or []
    shown = ", ".join(files[:40]) + (f" … (+{len(files) - 40} more)" if len(files) > 40 else "")
    footer = (
        f"[Nemotron 3.5 Super · {result.get('model_calls')} model calls · {result.get('seconds')} s · "
        f"status {result.get('status')}]"
    )
    return f"{result.get('answer', '').strip()}\n\nFiles read ({len(files)}): {shown or 'none'}\n{footer}"


def format_draft(result: dict[str, Any]) -> str:
    """Draft id, summary, files, commands and diff, in the order a reviewer needs them."""
    files = result.get("files") or []
    commands = result.get("commands") or []
    lines = [
        f"Draft {result['draft_id']} for {result['project']} — {len(files)} file(s) changed, status "
        f"{result.get('status')}, {result.get('model_calls')} model calls, {result.get('seconds')} s.",
        f"Summary: {str(result.get('summary') or '').strip()}",
        f"Files: {', '.join(files) or 'none'}",
        f"Commands run: {'; '.join(commands[-10:]) or 'none'}",
    ]
    if files:
        apply_hint = (
            f"Apply with nemotron_apply_draft(draft_id='{result['draft_id']}') or discard with nemotron_discard_draft."
        )
        lines += [f"Diff:\n{result.get('diff', '')}", apply_hint]
    else:
        lines.append("Nothing changed, so there is nothing to apply.")
    return "\n".join(lines)


class McpServer:
    """Stdio MCP server for delegation."""

    def __init__(self, out: TextIO | None = None) -> None:
        self.out = out or sys.stdout
        self._write_lock = threading.Lock()
        self._cancels: dict[Any, threading.Event] = {}
        self._threads: list[threading.Thread] = []

    def _write(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, default=str) + "\n"
        with self._write_lock:
            self.out.write(line)
            self.out.flush()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one request (or act on a notification, returning None)."""
        method = message.get("method")
        params = message.get("params") or {}
        req_id = message.get("id")
        if req_id is None:
            if method == "notifications/cancelled":
                event = self._cancels.get(params.get("requestId"))
                if event is not None:
                    event.set()
            return None
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            return self._ok(req_id, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return self._ok(req_id, {})
        if method == "tools/list":
            return self._ok(req_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._ok(req_id, self.call_tool(req_id, params))
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    @staticmethod
    def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def call_tool(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Run one tool. Failures come back as isError results, never as protocol errors."""
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")
        cancel = self._cancels.setdefault(req_id, threading.Event())
        count = [0]

        def progress(text: str) -> None:
            if token is None:
                return
            count[0] += 1
            self._write({"jsonrpc": "2.0", "method": "notifications/progress",
                         "params": {"progressToken": token, "progress": count[0], "message": text}})

        try:
            if name == "nemotron_explore":
                result = delegate.explore(
                    str(args.get("question") or ""), project=args.get("project_path"),
                    on_activity=progress, cancel=cancel,
                )
                return _text(format_explore(result), error=result.get("status") == "error")
            if name == "nemotron_draft_change":
                draft = delegate.create_draft(
                    str(args.get("project_path") or ""), str(args.get("task") or ""),
                    on_activity=progress, cancel=cancel,
                )
                return _text(format_draft(draft), error=draft.get("status") == "error")
            if name == "nemotron_apply_draft":
                applied = delegate.apply_draft(str(args.get("draft_id") or ""))
                if applied.get("applied"):
                    return _text(f"Applied to {applied['project']}: {', '.join(applied['files']) or 'no files'}.")
                return _text(f"Not applied: {applied.get('detail')} {json.dumps(applied.get('conflicts') or [])}",
                             error=True)
            if name == "nemotron_discard_draft":
                delegate.discard_draft(str(args.get("draft_id") or ""))
                return _text("Discarded. The project was never touched.")
            return _text(f"Unknown tool {name!r}.", error=True)
        except delegate.DelegateError as exc:
            return _text(str(exc), error=True)
        except Exception as exc:  # noqa: BLE001 — the client must always get a result
            return _text(f"{name} failed: {exc}\n{traceback.format_exc()[-600:]}", error=True)
        finally:
            self._cancels.pop(req_id, None)

    def serve(self, incoming: TextIO | None = None) -> int:
        """Read messages until stdin closes; tool calls run on their own threads."""
        for line in incoming or sys.stdin:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self._write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "tools/call" and message.get("id") is not None:
                self._cancels.setdefault(message["id"], threading.Event())
                thread = threading.Thread(target=self._run_call, args=(message,), daemon=True)
                self._threads.append(thread)
                thread.start()
                continue
            reply = self.handle(message)
            if reply is not None:
                self._write(reply)
        for event in self._cancels.values():
            event.set()
        return 0

    def _run_call(self, message: dict[str, Any]) -> None:
        reply = self.handle(message)
        if reply is not None:
            self._write(reply)


def main() -> int:
    """Entry point: python -m local_coder.mcp_server."""
    from .hostenv import apply_host_path

    apply_host_path()
    return McpServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
