"""Bounded development comparison: 4 curated tasks x 3 profiles (12 cells).

Profiles (non-thinking + final-channel throughout):
  C0: protocol v2 exact-replacement PATCH, existing verification policy.
  D1: protocol v2 exact-replacement PATCH, mandatory post-edit verification.
  D2: protocol v3 span-ID PATCH, mandatory post-edit verification.

Everything else is held constant (model file, runtime build, sampling,
retrieval, task/grader versions, budgets, isolated backend). Per-task order
is the predeclared rotation of [C0, D1, D2]. First outcomes only,
sequential, no held-out tasks. Raw episodes stay local; sanitized reports
are committed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from local_coder.benchmark import (  # noqa: E402
    aggregate,
    failure_category,
    host_available_mb,
    load_manifest,
    sanitize,
    task_paths,
    utc_now,
    verify_frozen,
)
from local_coder.controller import Controller  # noqa: E402
from local_coder.export import SKILL_ACCEPTED  # noqa: E402
from local_coder.persistence import EpisodeStore  # noqa: E402

MODEL_FILE = "/Users/nico-yardlogix/.cache/local-coder/models/Qwen_Qwen3.5-4B-Q4_K_M.gguf"
MODEL_ALIAS = "Qwen3.5-4B-Q4_K_M"
SERVER_URL = "http://127.0.0.1:8080"
SERVER_ARGV_BASE = [
    "/opt/homebrew/bin/llama-server", "-m", MODEL_FILE, "--port", "8080",
    "-c", "8192", "--n-gpu-layers", "99", "--alias", MODEL_ALIAS,
]

TASKS = ["fam-demo-calc", "fam-localization", "fam-multi-hunk", "fam-recovery"]
ORDER = ["C0", "D1", "D2"]

PROFILES = {
    "C0": {"protocol": "2", "constraint": "gbnf-action-v2", "verification": "off",
           "thinking": "off", "channel": "final-only", "server_thinking": "off"},
    "D1": {"protocol": "2", "constraint": "gbnf-action-v2", "verification": "mandatory",
           "thinking": "off", "channel": "final-only", "server_thinking": "off"},
    "D2": {"protocol": "3", "constraint": "gbnf-action-v3", "verification": "mandatory",
           "thinking": "off", "channel": "final-only", "server_thinking": "off"},
}

COMMON = {
    "model_file": MODEL_FILE, "model_alias": MODEL_ALIAS, "quantization": "Q4_K_M",
    "n_ctx": 8192, "temperature": 0.2, "max_tokens": 2048, "gen_timeout": 300,
    "execution": "isolated", "budgets": "defaults",
}


def code_sha() -> str:
    """Frozen code identity; refuses a dirty tree."""
    repo = REPO_ROOT
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    tracked = [line for line in dirty.splitlines()
               if not line.split()[-1].startswith(("var/", "pilot/reports/"))]
    if tracked:
        raise RuntimeError(f"refusing comparison on dirty tree: {tracked[:5]}")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    return sha


def ensure_server(mode: str, log: list[str]) -> None:
    """Restart the single local server with the recorded thinking mode."""
    subprocess.run(["pkill", "-f", "llama-server --port 8080"], capture_output=True)
    time.sleep(3)
    argv = list(SERVER_ARGV_BASE)
    if mode == "off":
        argv += ["--reasoning", "off"]
    elif mode != "auto":
        raise ValueError(f"unknown server thinking mode {mode!r}")
    log_path = Path(f"/tmp/llama-compare-{mode}.log")
    with log_path.open("wb") as handle:
        subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT)
    for _ in range(36):
        time.sleep(5)
        try:
            import urllib.request

            with urllib.request.urlopen(SERVER_URL + "/health", timeout=3) as response:
                if response.status == 200:
                    log.append(f"server mode={mode} argv={' '.join(argv)}")
                    return
        except OSError:
            pass
    raise RuntimeError(f"server did not become healthy in mode {mode!r}")


def build_adapter(profile: dict):
    """Live adapter for one comparison cell (no downloads, loopback only)."""
    from local_coder.inference.llama_cpp import LlamaCppAdapter

    return LlamaCppAdapter(
        SERVER_URL, model=MODEL_ALIAS, max_tokens=COMMON["max_tokens"],
        timeout_s=COMMON["gen_timeout"], model_file=MODEL_FILE,
        quantization=COMMON["quantization"], n_ctx=COMMON["n_ctx"],
        temperature=COMMON["temperature"], constraint=profile["constraint"],
        channel_policy=profile["channel"], thinking=profile["thinking"])


def span_offered_gold(store: EpisodeStore, episode_id: str, gold_files: list[str]) -> bool | None:
    """Whether any turn's span catalogue covered a gold file.

    None when the episode has no span bundles (non-v3 profiles). A False
    here is a span-coverage failure, never a code-generation failure.
    """
    import json as _json

    seen_bundle = False
    for turn in store.list_turns(episode_id):
        try:
            raw = store.read_turn_file(episode_id, turn, "spans.json")
        except OSError:
            continue
        seen_bundle = True
        try:
            bindings = _json.loads(raw)
        except ValueError:
            continue
        paths = set()
        if isinstance(bindings, dict):
            for value in bindings.values():
                if isinstance(value, dict) and "path" in value:
                    paths.add(value["path"])
        elif isinstance(bindings, list):
            for entry in bindings:
                if isinstance(entry, dict) and "path" in entry:
                    paths.add(entry["path"])
        if any(gold in paths for gold in gold_files):
            return True
    return False if seen_bundle else None


def run_cell(suite_path: Path, entry: dict, profile_name: str, profile: dict,
             store: EpisodeStore, run_id: str) -> dict:
    """Run one task/profile cell; first outcome kept."""
    from local_coder.config import load_config
    from local_coder.execution.isolated import IsolatedBackend
    from local_coder.skills.check import compare_to_baseline

    paths = task_paths(suite_path, entry)
    ticket = paths["ticket"].read_text(encoding="utf-8")
    cell: dict = {"id": entry["id"], "family": entry["family"], "split": entry["split"],
                  "origin": entry["origin"], "profile": profile_name,
                  "status": "scheduled"}
    drift = verify_frozen(suite_path, entry)
    if drift:
        cell.update({"status": "setup-failed", "detail": f"freeze drift: {drift}"})
        return cell
    episode_id = f"{run_id}-{entry['id']}-{profile_name}"
    started = time.monotonic()
    try:
        config = load_config(paths["repo"])
        controller = Controller(store, config, build_adapter(profile), IsolatedBackend(),
                                paths["repo"], ticket, episode_id=episode_id,
                                task_id=entry["id"], protocol_version=profile["protocol"],
                                verification_policy=profile["verification"])
        state = controller.run()
    except Exception as exc:  # noqa: BLE001 - recorded per-cell, never raised
        cell.update({"status": "setup-failed", "detail": str(exc)[:300]})
        return cell
    elapsed = round(time.monotonic() - started, 1)
    events = store.read_events(episode_id)
    observations = [e for e in events if e["type"] == "observation"]
    accepted = sum(1 for e in observations if e["data"].get("status") in SKILL_ACCEPTED)
    rejected_obs = sum(1 for e in observations
                       if e["data"].get("status") in ("rejected", "protocol_error", "no_progress"))
    # Rejection categories come from the turn bundles' observation.json
    # files (the complete record: skill rejections and budget/phase
    # rejections alike). patch_rejected events are a subset and are not
    # counted separately, to avoid double counting.
    patch_rejects: dict[str, int] = {}
    for turn in store.list_turns(episode_id):
        try:
            obs = json.loads(store.read_turn_file(episode_id, turn, "observation.json"))
        except (OSError, ValueError):
            continue
        if obs.get("status") == "rejected" and obs.get("code"):
            code = str(obs["code"])
            patch_rejects[code] = patch_rejects.get(code, 0) + 1
    proto_errors: dict[str, int] = {}
    for e in events:
        if e["type"] == "protocol_error":
            code = str(e["data"].get("code", "?"))
            proto_errors[code] = proto_errors.get(code, 0) + 1
    checked_changed = any(e["type"] == "check_intent"
                          and e["data"].get("tree") not in (None, state.source_tree_hash)
                          for e in events if e["type"] == "check_intent")
    evidence = state.final or state.current
    improvement: list[str] = []
    if state.baseline is not None and evidence is not None:
        improvement = compare_to_baseline(state.baseline, evidence).get("fixed", [])
    labels = json.loads(paths["labels"].read_text(encoding="utf-8"))
    gold_files = labels.get("files", [])
    focus = [f.path for f in state.focus] if state.focus else []
    mem = {}
    for e in events:
        if e["type"] == "handoff_written" and isinstance(e["data"].get("memory"), dict):
            mem = e["data"]["memory"]
    handoff_ok = None
    if state.outcome == "blocked":
        try:
            handoff = (store.episode_dir(episode_id) / "handoff.md").read_text(encoding="utf-8")
            handoff_ok = all(s in handoff for s in (
                "## Goal", "## Relevant files", "## Attempted changes",
                "## Latest failure", "## Why stopping"))
        except OSError:
            handoff_ok = False
    cell.update({
        "status": state.outcome,
        "category": failure_category(state.outcome, state.stop_reason),
        "stop_reason": state.stop_reason,
        "generations": state.generations_used,
        "patch_attempts": state.patch_attempts,
        "applied_edits": sum(1 for e in events if e["type"] == "patch_applied"),
        "skill_accepted": accepted,
        "skill_rejected": rejected_obs,
        "patch_rejections": patch_rejects,
        "protocol_errors": proto_errors,
        "checked_changed_code": checked_changed,
        "target_improvement": improvement,
        "model_checks": state.model_checks,
        "auto_checks": state.auto_checks,
        "test_invocations": state.test_invocations,
        "verification": profile["verification"],
        "interface": {"1": "unified-diff", "2": "replace", "3": "replace_span"}[profile["protocol"]],
        "span_offered_gold": span_offered_gold(store, episode_id, gold_files),
        "prompt_tokens_exact": state.prompt_tokens_exact,
        "completion_tokens_exact": state.completion_tokens_exact,
        "prompt_tokens_est": state.prompt_tokens_est,
        "completion_tokens_est": state.completion_tokens_est,
        "elapsed_s": elapsed,
        "episode_elapsed_s": round(state.elapsed_s, 1),
        "memory": mem,
        "regression": any(e["type"] == "check_completed" and e["data"].get("regression") == "True"
                          for e in events),
        "recovery": any(e["type"] == "patch_reverted" for e in events),
        "handoff_ok": handoff_ok,
        "recall_at_1": (1.0 if gold_files and any(f == gold_files[0] for f in focus[:1]) else 0.0)
        if gold_files else None,
        "recall_at_3": (len([f for f in gold_files if f in focus[:3]]) / len(gold_files))
        if gold_files else None,
        "human_interventions": 0,
        "provenance": {k: v for k, v in state.provenance.items()
                       if k in ("adapter", "model", "quantization", "n_ctx", "constraint",
                                "channel_policy", "thinking", "chat_template", "server_build",
                                "protocol_version", "execution_backend", "model_sha256")},
    })
    return cell


MAX_PROBES = 2

PROBE_PROMPTS = [
    ("check-final-channel",
     "Return exactly one JSON action object and nothing else: "
     '{"action":"check","scope":"focused"}'),
    ("span-shape",
     'File src/toy.py contains:\n'
     'def hello(name):\n    return "hi " + name\n'
     'Span [S1] covers this line exactly:     return "hi " + name\n'
     "Return exactly one JSON action object and nothing else: a patch replace_span "
     'action with span_id "S1" whose new text is     return "hello, " + name'),
]


def run_probes(report: dict, log: list[str]) -> None:
    """Run at most MAX_PROBES task-independent capability probes, recorded.

    Prompts mention no task, repo, or gold content. Each probe performs one
    real generation through the same thinking-off, final-only, v3-grammar
    configuration the D2 cells use. Failures are recorded, not retried.
    """
    from local_coder.inference.base import AdapterError

    probes: list[dict] = []
    for name, prompt in PROBE_PROMPTS[:MAX_PROBES]:
        adapter = build_adapter(PROFILES["D2"])
        try:
            record = adapter.probe(prompt=prompt, protocol_version="3")
            record["name"] = name
            record["outcome"] = "ok"
        except AdapterError as exc:
            record = {"name": name, "outcome": "failed",
                      "constraint": PROFILES["D2"]["constraint"],
                      "channel_policy": PROFILES["D2"]["channel"],
                      "thinking": PROFILES["D2"]["thinking"],
                      "detail": str(exc)[:300]}
        probes.append(record)
        log.append(f"probe {name}: {record['outcome']}")
    report["probes"] = sanitize(probes, os.path.expanduser("~"))


def main() -> None:
    """Freeze, run 12 cells sequentially, write sanitized reports."""
    suite_path = REPO_ROOT / "pilot" / "suite"
    manifest = load_manifest(suite_path)
    sha = code_sha()
    run_id = os.environ.get("COMPARE_RUN_ID", "compare-01")
    state_dir = REPO_ROOT / "var" / "episodes-compare"
    report_dir = REPO_ROOT / "pilot" / "reports" / run_id
    store = EpisodeStore(state_dir)
    home = os.path.expanduser("~")
    server_log: list[str] = []
    report: dict = {
        "report_version": 1, "run_id": run_id, "kind": "development-comparison",
        "suite": manifest["name"], "code_sha": sha,
        "common": COMMON, "profiles": PROFILES,
        "order": [f"{t}/{p}" for t in TASKS for p in ORDER],
        "started": utc_now(),
        "server_log": server_log, "tasks": [], "stopped_early": None,
    }
    entries = {e["id"]: e for e in manifest["entries"]}
    current_mode: str | None = None
    ensure_server(PROFILES["D2"]["server_thinking"], server_log)
    current_mode = PROFILES["D2"]["server_thinking"]
    run_probes(report, server_log)
    actual_order: list[str] = []
    for task_index, task_id in enumerate(TASKS):
        # Rotate the profile order per task so no profile always runs first
        # (fresh host) or last (pressured host); the rotation is predeclared.
        rotation = ORDER[task_index % len(ORDER):] + ORDER[:task_index % len(ORDER)]
        for profile_name in rotation:
            available = host_available_mb()
            if available is not None and available < 1024:
                report["stopped_early"] = f"host available {available}MB below 1024MB floor"
                break
            profile = PROFILES[profile_name]
            if profile["server_thinking"] != current_mode:
                ensure_server(profile["server_thinking"], server_log)
                current_mode = profile["server_thinking"]
            print(f"[{run_id}] {task_id} / {profile_name}", flush=True)
            actual_order.append(f"{task_id}/{profile_name}")
            report["tasks"].append(run_cell(suite_path, entries[task_id], profile_name,
                                            profile, store, run_id))
        if report.get("stopped_early"):
            break
    report["order"] = actual_order
    report["finished"] = utc_now()
    report["attempted"] = sum(1 for t in report["tasks"] if t["status"] != "scheduled")
    clean = sanitize(report, home)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps({**clean, "aggregate": aggregate(report)}, indent=2, sort_keys=True) + "\n")
    with (report_dir / "report.csv").open("w", newline="") as handle:
        import csv as _csv

        fields = ["id", "profile", "split", "status", "category", "generations", "patch_attempts",
                  "applied_edits", "checked_changed_code", "target_improvement", "model_checks",
                  "auto_checks", "test_invocations", "verification", "interface", "span_offered_gold",
                  "prompt_tokens_exact", "completion_tokens_exact", "elapsed_s",
                  "protocol_errors", "regression", "recovery", "handoff_ok", "recall_at_1"]
        writer = _csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for task in clean["tasks"]:
            row = dict(task)
            row["target_improvement"] = ";".join(task.get("target_improvement", []))
            row["protocol_errors"] = json.dumps(task.get("protocol_errors", {}), sort_keys=True)
            writer.writerow(row)
    lines = [f"# Development comparison {run_id}", "",
             f"Code: {sha} | order: {' '.join(report['order'])}", ""]
    for task_id in TASKS:
        lines.append(f"## {task_id}")
        for cell in [t for t in clean["tasks"] if t["id"] == task_id]:
            lines.append(
                f"- {cell['profile']}: {cell['status']} ({cell.get('category', '?')}) "
                f"gens={cell.get('generations', '-')} applied={cell.get('applied_edits', '-')} "
                f"checked_changed={cell.get('checked_changed_code', '-')} "
                f"improved={cell.get('target_improvement', [])} "
                f"rejects={cell.get('patch_rejections', {})} "
                f"verify={cell.get('verification', '?')} auto={cell.get('auto_checks', '-')} "
                f"spans={cell.get('span_offered_gold', '-')}")
        lines.append("")
    (report_dir / "report.md").write_text("\n".join(lines))
    (report_dir / "profiles.json").write_text(
        json.dumps(sanitize({"code_sha": sha, "common": COMMON, "profiles": PROFILES,
                             "server_log": server_log}, home), indent=2, sort_keys=True) + "\n")
    print(f"done: {len(report['tasks'])} cells")


if __name__ == "__main__":
    main()
