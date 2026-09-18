"""Prove host skill/instruction isolation without a live coding generation."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from local_coder.engines.context_budget import ConservativeEstimator, ContextPolicy
from local_coder.engines.llama_proxy import LlamaProxy
from local_coder.engines.observations import ObservationStore
from local_coder.engines.opencode_config import (
    ISOLATION_ENV_KEYS,
    isolated_env,
    opencode_config,
    write_engine_home,
)

MARKER_CLAUDE = "LOCAL_CODER_MARKER_CLAUDE_HOST_SHOULD_NOT_APPEAR"
MARKER_AGENTS = "LOCAL_CODER_MARKER_AGENTS_HOST_SHOULD_NOT_APPEAR"
MARKER_PARENT = "LOCAL_CODER_MARKER_PARENT_AGENTS_SHOULD_NOT_APPEAR"


def test_isolated_env_sets_supported_disable_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    env = isolated_env()
    for key in ISOLATION_ENV_KEYS:
        assert key in env, key
        if key.startswith("OPENCODE_DISABLE") or key == "OPENCODE_PURE":
            assert env[key] == "1"
    assert env["OPENCODE_TEST_HOME"] == env["HOME"]
    assert env["OPENCODE_TEST_HOME"].endswith("engine-home")
    assert "ANTHROPIC_API_KEY" not in env


def test_engine_home_does_not_write_into_trap_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    trap = tmp_path / "trap-home"
    (trap / ".claude").mkdir(parents=True)
    (trap / ".agents" / "skills" / "evil").mkdir(parents=True)
    (trap / ".claude" / "CLAUDE.md").write_text(MARKER_CLAUDE, encoding="utf-8")
    (trap / ".agents" / "skills" / "evil" / "SKILL.md").write_text(MARKER_AGENTS, encoding="utf-8")
    cfg = opencode_config(llama_base_url="http://127.0.0.1:9", model_id="Qwen3.5-9B-Q4_K_M")
    path = write_engine_home(cfg)
    assert MARKER_CLAUDE not in path.read_text(encoding="utf-8")
    assert (trap / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == MARKER_CLAUDE
    assert "Qwen3.5-9B-Q4_K_M" in path.read_text(encoding="utf-8")
    assert "Qwen3.5-4B-Q4_K_M" not in path.read_text(encoding="utf-8")


def test_proxy_strips_host_markers_from_assembled_request(tmp_path):
    """If OpenCode still serialized a host marker, the proxy must not leave a dump."""
    recorded: list[dict] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok","model_alias":"Qwen3.5-9B-Q4_K_M"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            recorded.append(json.loads(raw.decode("utf-8")))
            reply = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    store = ObservationStore(tmp_path / "obs")
    proxy = LlamaProxy(
        f"http://{host}:{port}",
        expected_model="Qwen3.5-9B-Q4_K_M",
        store=store,
        counter=ConservativeEstimator(),
    )
    proxy.start()
    try:
        import httpx
        dumped = MARKER_PARENT + "\n" + ("skill body\n" * 2_000)
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            response = client.post(
                f"{proxy.url}/v1/chat/completions",
                json={
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "messages": [
                        {"role": "system", "content": dumped},
                        {"role": "user", "content": "build"},
                    ],
                },
            )
        assert response.status_code == 200
        assert recorded
        sent = json.dumps(recorded[0])
        # A short marker in the excerpt may remain; the full dump must not.
        assert sent.count("skill body") < 400
        assert dumped not in sent
        assert proxy.dispatched == 1
        assert proxy.rejected == 0
    finally:
        proxy.stop()
        upstream.shutdown()


def test_proxy_refuses_irreducible_without_upstream_generation(tmp_path):
    seen = {"posts": 0}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            if "chat/completions" in self.path:
                seen["posts"] += 1
            self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    proxy = LlamaProxy(
        f"http://{host}:{port}",
        expected_model="Qwen3.5-9B-Q4_K_M",
        store=ObservationStore(tmp_path / "obs"),
        policy=ContextPolicy(n_ctx=800, output_reserve=100, safety_margin=20),
        counter=ConservativeEstimator(),
    )
    proxy.start()
    try:
        import httpx
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            response = client.post(
                f"{proxy.url}/v1/chat/completions",
                json={
                    "model": "Qwen3.5-9B-Q4_K_M",
                    "messages": [{"role": "user", "content": "USER-SPEC " * 20_000}],
                },
            )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "context_budget_error"
        assert seen["posts"] == 0
        assert proxy.dispatched == 0
        assert proxy.rejected == 1
    finally:
        proxy.stop()
        upstream.shutdown()


def test_isolated_home_is_not_the_real_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_DATA_DIR", str(tmp_path / "data"))
    env = isolated_env()
    assert Path(env["HOME"]) != Path.home()
    assert Path(env["OPENCODE_TEST_HOME"]) != Path.home()
    assert (Path.home() / ".claude") != (Path(env["HOME"]) / ".claude")


def test_opencode_serve_excludes_host_and_parent_markers(tmp_path, monkeypatch):
    """Start the owned OpenCode server against a mock llama. No live 9B generation."""
    from local_coder.engines import opencode_server as server_mod
    from local_coder.engines.opencode_config import engine_home, write_engine_home
    from local_coder.engines.opencode_server import OpenCodeServer, stop_owned
    from local_coder.machine import MachineConfig, write_machine_config

    write_machine_config(MachineConfig(
        model_alias="Qwen3.5-9B-Q4_K_M",
        n_ctx=32768,
        max_tokens=2048,
    ))

    parent = tmp_path / "workspace"
    project = parent / "app"
    project.mkdir(parents=True)
    (project / "README.md").write_text("demo\n", encoding="utf-8")
    (parent / "AGENTS.md").write_text(MARKER_PARENT + "\n", encoding="utf-8")
    (parent / "CLAUDE.md").write_text("LOCAL_CODER_MARKER_PARENT_CLAUDE\n", encoding="utf-8")
    (parent / "CONTEXT.md").write_text("LOCAL_CODER_MARKER_PARENT_CONTEXT\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("LOCAL_CODER_MARKER_PROJECT_AGENTS\n", encoding="utf-8")
    for rel, marker in ((".claude/skills/trap", MARKER_CLAUDE), (".agents/skills/trap", MARKER_AGENTS)):
        folder = parent / rel
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: trap\ndescription: {marker}\n---\n{marker}\n",
            encoding="utf-8",
        )

    original_write = write_engine_home

    def write_and_plant(config: dict) -> Path:
        path = original_write(config)
        home = engine_home()
        for rel, marker, name in (
            (".claude/skills/engine-trap", MARKER_CLAUDE, "engine-claude"),
            (".agents/skills/engine-trap", MARKER_AGENTS, "engine-agents"),
        ):
            folder = home / rel
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {marker}\n---\n{marker}\n",
                encoding="utf-8",
            )
        return path

    monkeypatch.setattr(server_mod, "write_engine_home", write_and_plant)

    recorded: list[dict] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if "models" in self.path:
                payload = {"data": [{"id": "Qwen3.5-9B-Q4_K_M", "object": "model"}]}
            else:
                payload = {"status": "ok", "model_alias": "Qwen3.5-9B-Q4_K_M"}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            if "tokenize" in self.path:
                text = str(payload.get("content") or "")
                count = max(1, (len(text.encode("utf-8")) + 1) // 2)
                reply = {"tokens": [0] * min(count, 50_000)}
            else:
                recorded.append(payload)
                reply = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
                }
            body = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address[:2]
    server = OpenCodeServer(
        project,
        llama_endpoint=f"http://{host}:{port}",
        model_id="Qwen3.5-9B-Q4_K_M",
        context=32768,
        output=256,
    )
    try:
        started = server.start(wait_s=25)
        assert started["model_id"] == "Qwen3.5-9B-Q4_K_M"
        client = server.client()
        effective = client.config()
        blob = json.dumps(effective)
        assert "Qwen3.5-9B-Q4_K_M" in str(effective.get("model"))
        assert "Qwen3.5-4B-Q4_K_M" not in blob
        for marker in (
            MARKER_CLAUDE,
            MARKER_AGENTS,
            MARKER_PARENT,
            "LOCAL_CODER_MARKER_PARENT_CLAUDE",
            "LOCAL_CODER_MARKER_PARENT_CONTEXT",
            "LOCAL_CODER_MARKER_PROJECT_AGENTS",
        ):
            assert marker not in blob
        session = client.create_session(title="isolation")
        session_id = str(session.get("id") or session.get("sessionID") or "")
        assert session_id
        client.prompt_async(
            session_id,
            "Reply with the word ready.",
            provider_id="llama.cpp",
            model_id="Qwen3.5-9B-Q4_K_M",
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not recorded:
            time.sleep(0.2)
        assert recorded, "OpenCode never sent a chat request to the mock llama"
        assembled = json.dumps(recorded)
        for marker in (
            MARKER_CLAUDE,
            MARKER_AGENTS,
            MARKER_PARENT,
            "LOCAL_CODER_MARKER_PARENT_CLAUDE",
            "LOCAL_CODER_MARKER_PARENT_CONTEXT",
            "LOCAL_CODER_MARKER_PROJECT_AGENTS",
        ):
            assert marker not in assembled
        assert "Qwen3.5-4B-Q4_K_M" not in assembled
    finally:
        stop_owned()
        upstream.shutdown()
