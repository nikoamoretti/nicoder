"""Bounded comparison: D1 (v2b exact-text) vs D2-fixed (v3b span catalogue).

Both profiles use mandatory post-edit verification, the verified non-thinking
configuration, final-channel-only actions, and the shared transport
hardening. Caps are enforced in this runner: at most 8 task episodes (4
curated development tasks x 2 profiles) and at most 2 task-independent
probe attempts (one per profile; failures count against the cap, no retry).

Order is a predeclared balanced interleave per task: [D1, D2] alternating
with [D2, D1]. First outcomes only; no rerolls; code and profiles are frozen
by commit before the run (code_sha refuses a dirty tree). Raw episodes stay
local; sanitized reports are committed.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_dev import build_adapter, code_sha, ensure_server, utc_now  # noqa: E402

from local_coder.benchmark import (  # noqa: E402
    aggregate,
    failure_category,
    host_available_mb,
    load_manifest,
    sanitize,
    task_paths,
    verify_frozen,
)
from local_coder.controller import Controller  # noqa: E402
from local_coder.inference.grammar import constraint_id_v2, constraint_id_v3  # noqa: E402
from local_coder.persistence import EpisodeStore  # noqa: E402
from local_coder.spans import SPAN_CATALOGUE_VERSION  # noqa: E402

TASKS = ["fam-demo-calc", "fam-localization", "fam-multi-hunk", "fam-recovery"]
MAX_CELLS = 8
MAX_PROBES = 2

PROFILES = {
    "D1": {"protocol": "2", "constraint": constraint_id_v2(), "verification": "mandatory",
           "thinking": "off", "channel": "final-only", "server_thinking": "off",
           "interface": "replace"},
    "D2": {"protocol": "3", "constraint": constraint_id_v3(), "verification": "mandatory",
           "thinking": "off", "channel": "final-only", "server_thinking": "off",
           "interface": "replace_span"},
}

PROBE_PROMPTS = {
    "D1": ("check-final-channel",
           "Return exactly one JSON action object and nothing else: "
           '{"action":"check","scope":"focused"}'),
    "D2": ("span-shape",
           "A toy file src/toy.py contains:\n"
           "def hello(name):\n    return \"hi \" + name\n"
           "Its full function body was offered as span S1.\n"
           "Return exactly one JSON action object and nothing else: a patch replace_span "
           'action with span_id "S1" whose new text is     return "hello, " + name'),
}


def _line_offsets(data: bytes) -> list[int]:
    """Start byte of every 1-based line."""
    offsets = [0]
    for index, byte in enumerate(data):
        if byte == 0x0A:
            offsets.append(index + 1)
    return offsets


def gold_changed_lines(gold_diff: str) -> dict[str, set[int]]:
    """Old-file line numbers deleted or replaced by the gold patch."""
    changed: dict[str, set[int]] = {}
    current: str | None = None
    old_lineno = 0
    for raw in gold_diff.splitlines():
        if raw.startswith("--- "):
            current = None
        elif raw.startswith("+++ "):
            path = raw[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current = path
            changed.setdefault(path, set())
        elif raw.startswith("@@ ") and current is not None:
            old = raw.split("@@")[1].strip().split()[0][1:]
            old_lineno = int(old.split(",")[0])
        elif current is not None and raw[:1] == " ":
            old_lineno += 1
        elif current is not None and raw[:1] == "-":
            changed[current].add(old_lineno)
            old_lineno += 1
    return changed


def gold_unit_covered(root: Path, changed: dict[str, set[int]],
                      spans: list[dict]) -> bool:
    """Offline evaluation only: does any offered span overlap a gold change?"""
    for path, lines in changed.items():
        if not lines or not (root / path).exists():
            continue
        data = (root / path).read_bytes()
        offsets = _line_offsets(data)
        intervals = []
        for line in sorted(lines):
            if 1 <= line <= len(offsets):
                start = offsets[line - 1]
                end = offsets[line] if line < len(offsets) else len(data)
                intervals.append((start, end))
        for span in spans:
            if span.get("path") != path:
                continue
            for start, end in intervals:
                if span.get("start_byte", 0) < end and start < span.get("end_byte", 0):
                    return True
    return False


def _first_spans(store: EpisodeStore, episode_id: str) -> list[dict]:
    for turn in store.list_turns(episode_id):
        try:
            raw = store.read_turn_file(episode_id, turn, "spans.json")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, list) and data:
            return data
    return []


def run_probe(profile_name: str, profile: dict, log: list[str]) -> dict:
    """One task-independent probe per profile; failure is recorded, not retried."""
    from local_coder.inference.base import AdapterError

    name, prompt = PROBE_PROMPTS[profile_name]
    adapter = build_adapter(profile)
    try:
        record = adapter.probe(prompt=prompt, protocol_version=profile["protocol"])
        record.update({"name": name, "profile": profile_name, "outcome": "ok"})
    except AdapterError as exc:
        record = {"name": name, "profile": profile_name, "outcome": "failed",
                  "constraint": profile["constraint"],
                  "channel_policy": profile["channel"],
                  "thinking": profile["thinking"],
                  "detail": str(exc)[:300],
                  "diagnostics": getattr(exc, "diagnostics", None)}
    log.append(f"probe {profile_name}/{name}: {record['outcome']}")
    return record


def run_cell(suite_path: Path, entry: dict, profile_name: str, profile: dict,
             store: EpisodeStore, run_id: str) -> dict:
    """Run one task/profile cell; first outcome kept, all failures recorded."""
    from local_coder.config import load_config
    from local_coder.execution.isolated import IsolatedBackend
    from local_coder.skills.check import compare_to_baseline

    paths = task_paths(suite_path, entry)
    ticket = paths["ticket"].read_text(encoding="utf-8")
    cell: dict = {"id": entry["id"], "family": entry["family"], "split": entry["split"],
                  "origin": entry["origin"], "profile": profile_name,
                  "interface": profile["interface"], "status": "scheduled"}
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
    accepted = sum(1 for e in observations if e["data"].get("status") in ("ok", "applied"))
    rejected_obs = sum(1 for e in observations
                       if e["data"].get("status") in ("rejected", "protocol_error", "no_progress"))
    patch_rejects: dict[str, int] = {}
    applied_checks: list[str] = []
    for turn in store.list_turns(episode_id):
        try:
            obs = json.loads(store.read_turn_file(episode_id, turn, "observation.json"))
        except (OSError, ValueError):
            continue
        if obs.get("status") == "rejected" and obs.get("code"):
            code = str(obs["code"])
            patch_rejects[code] = patch_rejects.get(code, 0) + 1
        if obs.get("status") == "applied":
            auto = obs.get("auto_check") or {}
            applied_checks.append(str(auto.get("status", "unchecked")))
    proto_errors: dict[str, int] = {}
    transport_errors = 0
    for e in events:
        if e["type"] == "protocol_error":
            code = str(e["data"].get("code", "?"))
            proto_errors[code] = proto_errors.get(code, 0) + 1
        if e["type"] == "adapter_error":
            transport_errors += 1
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
    offered_spans = _first_spans(store, episode_id)
    offered_paths = {s.get("path") for s in offered_spans}
    gold_root = paths["repo"]
    changed = gold_changed_lines(paths["gold"].read_text(encoding="utf-8"))
    read_only_omitted = 0
    try:
        briefing = store.read_turn_file(episode_id, 1, "briefing.txt")
        read_only_omitted = briefing.count("read-only path: no editable spans")
    except OSError:
        pass
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
        "applied_edit_check_results": applied_checks,
        "skill_accepted": accepted,
        "skill_rejected": rejected_obs,
        "patch_rejections": patch_rejects,
        "protocol_errors": proto_errors,
        "transport_errors": transport_errors,
        "checked_changed_code": checked_changed,
        "target_improvement": improvement,
        "model_checks": state.model_checks,
        "auto_checks": state.auto_checks,
        "test_invocations": state.test_invocations,
        "verification": profile["verification"],
        "offered_spans": len(offered_spans),
        "offered_span_paths": sorted(p for p in offered_paths if p),
        "read_only_omitted": read_only_omitted,
        "span_file_offered": (any(f in offered_paths for f in gold_files)
                              if gold_files else None),
        "useful_unit_offered": (gold_unit_covered(gold_root, changed, offered_spans)
                                if offered_spans and changed else None),
        "prompt_tokens_exact": state.prompt_tokens_exact,
        "completion_tokens_exact": state.completion_tokens_exact,
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
                                "channel_policy", "thinking", "transport_policy",
                                "transport_max_bytes", "event_max_bytes", "decoded_max_bytes",
                                "chat_template", "server_build", "protocol_version",
                                "execution_backend", "model_sha256")},
    })
    return cell


def main() -> None:
    """Freeze check, 2 probes, 8 cells max, sanitized reports."""
    suite_path = REPO_ROOT / "pilot" / "suite"
    manifest = load_manifest(suite_path)
    sha = code_sha()
    run_id = os.environ.get("COMPARE_RUN_ID", "compare-03")
    state_dir = REPO_ROOT / "var" / "episodes-compare-03"
    report_dir = REPO_ROOT / "pilot" / "reports" / run_id
    store = EpisodeStore(state_dir)
    home = os.path.expanduser("~")
    server_log: list[str] = []
    report: dict = {
        "report_version": 1, "run_id": run_id, "kind": "bounded-fixed-comparison",
        "suite": manifest["name"], "code_sha": sha,
        "catalogue_version": SPAN_CATALOGUE_VERSION,
        "profiles": PROFILES, "max_cells": MAX_CELLS, "max_probes": MAX_PROBES,
        "started": utc_now(), "server_log": server_log, "tasks": [], "probes": [],
        "stopped_early": None,
    }
    entries = {e["id"]: e for e in manifest["entries"]}
    ensure_server(PROFILES["D2"]["server_thinking"], server_log)
    probe_count = 0
    for profile_name in ("D1", "D2"):
        if probe_count >= MAX_PROBES:
            break
        report["probes"].append(run_probe(profile_name, PROFILES[profile_name], server_log))
        probe_count += 1
    actual_order: list[str] = []
    cell_count = 0
    for task_index, task_id in enumerate(TASKS):
        rotation = ["D1", "D2"] if task_index % 2 == 0 else ["D2", "D1"]
        for profile_name in rotation:
            if cell_count >= MAX_CELLS:
                report["stopped_early"] = f"cell cap {MAX_CELLS} reached"
                break
            available = host_available_mb()
            if available is not None and available < 1024:
                report["stopped_early"] = f"host available {available}MB below 1024MB floor"
                break
            print(f"[{run_id}] {task_id} / {profile_name}", flush=True)
            actual_order.append(f"{task_id}/{profile_name}")
            report["tasks"].append(run_cell(suite_path, entries[task_id], profile_name,
                                            PROFILES[profile_name], store, run_id))
            cell_count += 1
        if report.get("stopped_early"):
            break
    report["order"] = actual_order
    report["finished"] = utc_now()
    report["attempted"] = sum(1 for t in report["tasks"] if t["status"] != "scheduled")
    report["cell_count"] = len(report["tasks"])
    report["probe_count"] = len(report["probes"])
    clean = sanitize(report, home)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps({**clean, "aggregate": aggregate(report)}, indent=2, sort_keys=True) + "\n")
    with (report_dir / "report.csv").open("w", newline="") as handle:
        import csv as _csv

        fields = ["id", "profile", "interface", "split", "status", "category", "generations",
                  "patch_attempts", "applied_edits", "applied_edit_check_results",
                  "checked_changed_code", "target_improvement", "model_checks", "auto_checks",
                  "test_invocations", "offered_spans", "read_only_omitted", "span_file_offered",
                  "useful_unit_offered", "prompt_tokens_exact", "completion_tokens_exact",
                  "elapsed_s", "transport_errors", "regression", "recovery", "handoff_ok",
                  "recall_at_1"]
        writer = _csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for task in clean["tasks"]:
            row = dict(task)
            row["target_improvement"] = ";".join(task.get("target_improvement", []))
            row["applied_edit_check_results"] = ";".join(task.get("applied_edit_check_results", []))
            row["patch_rejections"] = json.dumps(task.get("patch_rejections", {}), sort_keys=True)
            row["protocol_errors"] = json.dumps(task.get("protocol_errors", {}), sort_keys=True)
            writer.writerow(row)
    lines = [f"# Bounded comparison {run_id}", "",
             f"Code: {sha} | catalogue: {SPAN_CATALOGUE_VERSION}",
             f"Order: {' '.join(actual_order)}",
             f"Probes: {report['probe_count']}/{MAX_PROBES} | cells: {report['cell_count']}/{MAX_CELLS}",
             ""]
    for probe in clean["probes"]:
        lines.append(f"- probe {probe['profile']}/{probe['name']}: {probe['outcome']} "
                     f"({probe.get('detail', 'ok')})")
    lines.append("")
    for task_id in TASKS:
        lines.append(f"## {task_id}")
        for cell in [t for t in clean["tasks"] if t["id"] == task_id]:
            lines.append(
                f"- {cell['profile']}: {cell['status']} ({cell.get('category', '?')}) "
                f"gens={cell.get('generations', '-')} applied={cell.get('applied_edits', '-')} "
                f"checks={cell.get('applied_edit_check_results', [])} "
                f"improved={cell.get('target_improvement', [])} "
                f"rejects={cell.get('patch_rejections', {})} "
                f"auto={cell.get('auto_checks', '-')} spans={cell.get('offered_spans', '-')} "
                f"unit={cell.get('useful_unit_offered', '-')}")
        lines.append("")
    (report_dir / "report.md").write_text("\n".join(lines))
    (report_dir / "profiles.json").write_text(
        json.dumps(sanitize({"code_sha": sha, "catalogue_version": SPAN_CATALOGUE_VERSION,
                             "profiles": PROFILES, "server_log": server_log}, home),
                   indent=2, sort_keys=True) + "\n")
    print(f"done: {report['cell_count']} cells, {report['probe_count']} probes")


if __name__ == "__main__":
    main()