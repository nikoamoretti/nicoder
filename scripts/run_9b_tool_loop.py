"""Re-run the 9B OpenCode tool loop against the owned llama-server."""
from __future__ import annotations

import json
import time
from pathlib import Path

from local_coder.engines.opencode_server import OpenCodeServer, stop_owned
from local_coder.machine import load_machine_config
from local_coder.server import server_status


def main() -> int:
    out = Path.home() / ".local/share/local-coder/evidence/9b-taskboard-2026-09-17/probes-9b"
    project = Path.home() / ".local/share/local-coder/evidence/9b-taskboard-2026-09-17/probe-project-9b"
    out.mkdir(parents=True, exist_ok=True)
    if project.exists():
        for child in project.rglob("*"):
            if child.is_file():
                child.unlink()
    project.mkdir(parents=True, exist_ok=True)
    (project / "src").mkdir(exist_ok=True)
    cfg = load_machine_config()
    status = server_status(cfg)
    print("llama", json.dumps(status), flush=True)
    print("stopping leftover OpenCode", stop_owned(), flush=True)
    server = OpenCodeServer(project, llama_endpoint=cfg.endpoint, model_id=cfg.model_alias)
    started = server.start(wait_s=40)
    print("opencode", json.dumps(started, default=str), flush=True)
    client = server.client()
    config = client.config()
    print("config_model", config.get("model"), flush=True)
    session = client.create_session(title="compat-tool-loop-9b")
    session_id = str(session.get("id") or session.get("sessionID") or "")
    prompt = (
        "Using your tools, create src/math_util.ts exporting add(a: number, b: number) "
        "that initially returns a - b. Then change it so add(2, 3) is 5. Write "
        "src/math_util.test.ts that asserts add(2, 3) === 5 using a simple throw if false, "
        "run it with node, and fix any real error. Do not explain without using tools."
    )
    t0 = time.time()
    client.prompt_async(session_id, prompt, provider_id="llama.cpp", model_id=cfg.model_alias)
    events: list[dict] = []
    try:
        for event in client.events():
            events.append({"t": round(time.time() - t0, 2), "type": event.get("type")})
            etype = str(event.get("type") or "")
            print(f"{time.time() - t0:6.1f}s {etype}", flush=True)
            if "idle" in etype or "error" in etype or time.time() - t0 > 240:
                break
    except Exception as exc:  # noqa: BLE001 — probe records the failure
        events.append({"error": str(exc)})
    created = (project / "src" / "math_util.ts").exists()
    text = (project / "src" / "math_util.ts").read_text(encoding="utf-8") if created else ""
    payload = {
        "session_id": session_id,
        "seconds": round(time.time() - t0, 2),
        "file_created": created,
        "looks_like_add": "a + b" in text or "a+b" in text,
        "file_excerpt": text[:400],
        "config_model": config.get("model"),
        "events": events[:60],
    }
    (out / "tool_loop_9b.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("seconds", "file_created", "looks_like_add", "config_model")}, indent=2))
    return 0 if created and payload["looks_like_add"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
