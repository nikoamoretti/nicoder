"""Compatibility probes for the pinned OpenCode + local llama.cpp path.

These are plumbing checks, not the application-quality demonstration.
Results are written under the given --out directory.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from local_coder.engines.opencode_client import OpenCodeError
from local_coder.engines.opencode_config import isolated_env, opencode_config
from local_coder.engines.opencode_pin import pin_record
from local_coder.engines.opencode_server import OpenCodeServer, stop_owned
from local_coder.machine import load_machine_config
from local_coder.server import server_start, server_status


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def probe_server(out: Path, project: Path) -> dict:
    """Start isolated OpenCode and inspect health, config, and providers."""
    cfg = load_machine_config()
    server = OpenCodeServer(project, llama_endpoint=cfg.endpoint, model_id=cfg.model_alias)
    started = server.start(wait_s=40)
    client = server.client()
    health = client.health()
    config = client.config()
    providers = client.providers()
    path_info = client.path()
    env = isolated_env()
    result = {
        "probe": "server_isolation",
        "pin": pin_record(),
        "started": started,
        "health": health,
        "path": path_info,
        "config_model": config.get("model") if isinstance(config, dict) else None,
        "enabled_providers": config.get("enabled_providers") if isinstance(config, dict) else None,
        "share": config.get("share") if isinstance(config, dict) else None,
        "autoupdate": config.get("autoupdate") if isinstance(config, dict) else None,
        "connected_providers": providers.get("connected") if isinstance(providers, dict) else None,
        "isolated_home": env["HOME"],
        "user_opencode_config_exists": Path.home().joinpath(".config/opencode/opencode.jsonc").exists(),
        "homes_differ": env["HOME"] != str(Path.home()),
    }
    write_json(out / "probe_server.json", result)
    return result


def probe_tools(out: Path) -> dict:
    """List native tools for the local model via the installed OpenAPI."""
    from local_coder.engines.opencode_server import client_from_record

    client = client_from_record()
    cfg = load_machine_config()
    tools = client.experimental_tools("llama.cpp", cfg.model_alias)
    names: list[str] = []
    if isinstance(tools, list):
        for row in tools:
            if isinstance(row, dict):
                names.append(str(row.get("id") or row.get("name") or row))
    elif isinstance(tools, dict):
        names = [str(k) for k in tools.keys()]
    result = {"probe": "native_tools", "tools": names, "raw_type": type(tools).__name__}
    write_json(out / "probe_tools.json", result)
    return result


def probe_tool_loop(out: Path, project: Path) -> dict:
    """Ask the local model to create a file, change it, run a check, fix a failure."""
    from local_coder.engines.opencode_server import client_from_record

    cfg = load_machine_config()
    status = server_status(cfg)
    if not status.get("healthy"):
        server_start(cfg, wait_s=120)
        status = server_status(cfg)
    client = client_from_record()
    session = client.create_session(title="compat-tool-loop")
    session_id = str(session.get("id") or session.get("sessionID") or "")
    prompt = (
        "Using your tools, create src/math_util.ts exporting add(a: number, b: number) "
        "that initially returns a - b. Then change it so add(2, 3) is 5. Write "
        "src/math_util.test.ts that asserts add(2, 3) === 5 using a simple throw if false, "
        "run it with node, and fix any real error. Do not explain without using tools."
    )
    events: list[dict] = []
    t0 = time.time()
    client.prompt_async(session_id, prompt, provider_id="llama.cpp", model_id=cfg.model_alias)
    try:
        for event in client.events():
            events.append({"t": round(time.time() - t0, 2), "event": event})
            etype = str(event.get("type") or event.get("event") or "")
            if "idle" in etype or time.time() - t0 > 180:
                break
    except OpenCodeError as exc:
        events.append({"error": exc.detail})
    created = (project / "src" / "math_util.ts").exists()
    text = (project / "src" / "math_util.ts").read_text(encoding="utf-8") if created else ""
    result = {
        "probe": "tool_loop",
        "session_id": session_id,
        "seconds": round(time.time() - t0, 2),
        "file_created": created,
        "file_excerpt": text[:400],
        "looks_like_add": "a + b" in text or "a+b" in text,
        "event_count": len(events),
        "event_types": [
            str(e.get("event", {}).get("type") or e.get("event", {}).get("event"))
            for e in events[:40]
        ],
    }
    write_json(out / "probe_tool_loop.json", result)
    write_json(out / "probe_tool_loop_events.json", events[:80])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--with-model", action="store_true")
    args = parser.parse_args()
    out = Path(args.out)
    project = Path(args.project)
    project.mkdir(parents=True, exist_ok=True)
    (project / "src").mkdir(exist_ok=True)
    try:
        server = probe_server(out, project)
        tools = probe_tools(out)
        payload = {"server": server, "tools": tools}
        if args.with_model:
            payload["tool_loop"] = probe_tool_loop(out, project)
        write_json(out / "probe_summary.json", payload)
        print(json.dumps({"ok": True, "out": str(out), "pin": pin_record()}, indent=2))
        return 0
    finally:
        if not args.with_model:
            stop_owned()


if __name__ == "__main__":
    raise SystemExit(main())
