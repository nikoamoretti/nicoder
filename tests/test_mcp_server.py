"""The delegation MCP server: handshake, tool list, results, progress, cancellation."""
from __future__ import annotations

import io
import json
import threading
import time

from local_coder import mcp_server
from local_coder.agent import delegate
from local_coder.mcp_server import McpServer


def _request(req_id, method, params=None) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


def test_handshake_and_tool_list():
    server = McpServer(out=io.StringIO())
    init = server.handle(_request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "local-coder"
    assert "Nemotron" in init["result"]["instructions"]
    old = server.handle(_request(2, "initialize", {"protocolVersion": "1999-01-01"}))
    assert old["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [tool["name"] for tool in server.handle(_request(3, "tools/list"))["result"]["tools"]]
    assert names == ["nemotron_explore", "nemotron_draft_change", "nemotron_apply_draft", "nemotron_discard_draft"]
    assert server.handle(_request(4, "ping"))["result"] == {}
    assert server.handle(_request(5, "resources/list"))["error"]["code"] == -32601


def test_explore_result_and_errors(monkeypatch):
    def fake_explore(question, *, project, on_activity, cancel):
        on_activity("Reading src/a.ts")
        return {"answer": "It is in src/a.ts:3.", "status": "answered", "files_read": ["~/p/src/a.ts"],
                "model_calls": 2, "seconds": 4.2}

    monkeypatch.setattr(delegate, "explore", fake_explore)
    server = McpServer(out=io.StringIO())
    reply = server.handle(_request(7, "tools/call", {"name": "nemotron_explore", "arguments": {"question": "where?"}}))
    result = reply["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert text.startswith("It is in src/a.ts:3.")
    assert "Files read (1): ~/p/src/a.ts" in text and "2 model calls" in text

    def refused(*args, **kwargs):
        raise delegate.DelegateError("/tmp/x must be a project folder inside /Users/me.")

    monkeypatch.setattr(delegate, "explore", refused)
    bad = server.handle(_request(8, "tools/call", {"name": "nemotron_explore", "arguments": {"question": "q"}}))
    assert bad["result"]["isError"] is True and "must be a project folder" in bad["result"]["content"][0]["text"]
    unknown = server.handle(_request(9, "tools/call", {"name": "nope", "arguments": {}}))
    assert unknown["result"]["isError"] is True


def test_draft_result_lists_what_to_review(monkeypatch):
    draft = {"draft_id": "d20261022-120000-abcd", "project": "/p", "status": "answered", "summary": "Set 12.",
             "files": ["src/price.ts"], "commands": ["npm test"], "model_calls": 3, "seconds": 9.0,
             "diff": "-10\n+12", "state": "open"}
    monkeypatch.setattr(delegate, "create_draft", lambda *a, **k: draft)
    monkeypatch.setattr(delegate, "apply_draft", lambda draft_id: {"applied": True, "project": "/p",
                                                                     "files": ["src/price.ts"]})
    server = McpServer(out=io.StringIO())
    made = server.handle(_request(1, "tools/call", {"name": "nemotron_draft_change",
                                                    "arguments": {"project_path": "/p", "task": "12"}}))
    text = made["result"]["content"][0]["text"]
    assert "Draft d20261022-120000-abcd" in text and "Files: src/price.ts" in text and "Commands run: npm test" in text
    assert "Diff:\n-10\n+12" in text and "nemotron_apply_draft(draft_id='d20261022-120000-abcd')" in text
    applied = server.handle(_request(2, "tools/call", {"name": "nemotron_apply_draft",
                                                       "arguments": {"draft_id": "d20261022-120000-abcd"}}))
    assert applied["result"]["content"][0]["text"] == "Applied to /p: src/price.ts."


def test_serve_streams_progress_and_honors_cancel(monkeypatch):
    started = threading.Event()

    def slow_explore(question, *, project, on_activity, cancel):
        on_activity("Looking in ~/p")
        started.set()
        cancel.wait(5)
        return {"answer": "Stopped.", "status": "stopped", "files_read": [], "model_calls": 1, "seconds": 0.1}

    monkeypatch.setattr(delegate, "explore", slow_explore)

    class Feed(io.StringIO):
        """Delivers the cancel only after the call has started, like a real client."""

        def __iter__(self):
            yield json.dumps(_request(11, "tools/call", {
                "name": "nemotron_explore", "arguments": {"question": "q"}, "_meta": {"progressToken": "tok"},
            })) + "\n"
            started.wait(5)
            yield json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                              "params": {"requestId": 11}}) + "\n"
            yield json.dumps(_request(12, "ping")) + "\n"

    out = io.StringIO()
    server = McpServer(out=out)
    server.serve(Feed())
    for thread in server._threads:
        thread.join(10)
    messages = [json.loads(row) for row in out.getvalue().splitlines()]
    progress = [row for row in messages if row.get("method") == "notifications/progress"]
    assert progress and progress[0]["params"] == {"progressToken": "tok", "progress": 1, "message": "Looking in ~/p"}
    answer = next(row for row in messages if row.get("id") == 11)
    assert "Stopped." in answer["result"]["content"][0]["text"]
    order = [row.get("id") for row in messages if row.get("id") is not None]
    assert order.index(12) < order.index(11)
    assert time.monotonic()
