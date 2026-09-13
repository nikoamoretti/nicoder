"""Pilot benchmark: frozen suite validation, sequential runs, reports.

Suite tasks are frozen by manifest hashes (repo tree, ticket, tests, gold).
Validation proves, offline: broken fixtures fail, gold solutions pass, and
null/wrong controls fail — with gold material unreachable from task repos.
Live runs are sequential, first-outcome-only, on the isolated backend, with
a resource gate that stops cleanly under sustained pressure.

Reports (JSON/CSV/Markdown) are sanitized for commit: relative paths only,
no absolute host paths, no environment dumps. Raw episodes stay local.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

SUITE_VERSION = 1
REPORT_VERSION = 1
MIN_HOST_AVAILABLE_MB = 1024


def utc_now() -> str:
    """Current UTC timestamp."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_manifest(suite_path: Path) -> dict:
    """Load and sanity-check a suite manifest."""
    manifest = json.loads((suite_path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("suite_version") != SUITE_VERSION:
        raise ValueError(f"unsupported suite_version {manifest.get('suite_version')}")
    dev = set(manifest["splits"]["dev"])
    held = set(manifest["splits"]["held-out"])
    ids = [e["id"] for e in manifest["entries"]]
    if set(ids) != dev | held or dev & held:
        raise ValueError("split ids do not partition entries")
    families: dict[str, str] = {}
    for entry in manifest["entries"]:
        if entry["family"] in families and families[entry["family"]] != entry["split"]:
            raise ValueError(f"family {entry['family']} leaks across splits")
        families[entry["family"]] = entry["split"]
    return manifest


def task_paths(suite_path: Path, entry: dict) -> dict[str, Path]:
    """Resolve task files relative to the suite directory's repo root."""
    root = suite_path.parent.parent
    return {
        "repo": root / entry["repo"],
        "ticket": root / entry["ticket"],
        "gold": root / entry["gold"],
        "wrong": root / entry["wrong"] if entry.get("wrong") else None,
        "labels": root / entry["labels"],
    }


def verify_frozen(suite_path: Path, entry: dict) -> list[str]:
    """Recompute freeze hashes. Returns a list of drift descriptions."""
    from .config import load_config
    from .paths import original_manifest, tree_hash

    drift = []
    paths = task_paths(suite_path, entry)
    manifest = original_manifest(paths["repo"])
    if tree_hash(manifest) != entry["hashes"]["repo_tree"]:
        drift.append("repo_tree")
    ticket = paths["ticket"].read_text(encoding="utf-8")
    if hashlib.sha256(ticket.encode()).hexdigest()[:16] != entry["hashes"]["ticket_sha"]:
        drift.append("ticket")
    for rel, want in entry["hashes"]["tests"].items():
        if manifest.get(rel) != want:
            drift.append(f"test:{rel}")
    gold = paths["gold"].read_text(encoding="utf-8")
    if hashlib.sha256(gold.encode()).hexdigest()[:16] != entry["hashes"]["gold_sha"]:
        drift.append("gold")
    config = load_config(paths["repo"])
    if config.registry_hash() != entry["env"]["registry_hash"]:
        drift.append("registry")
    return drift


def gold_hidden_from_repo(suite_path: Path, entry: dict) -> list[str]:
    """Gold/hidden material must be unreachable from the task repo."""
    paths = task_paths(suite_path, entry)
    leaked = []
    for path in sorted(paths["repo"].rglob("*")):
        name = path.name
        if (name.endswith((".diff", ".gold")) or name in ("gold.json", "wrong.diff", "provenance.json")
                or "gold" in name or "hidden" in name):
            leaked.append(path.relative_to(paths["repo"]).as_posix())
    return leaked


def _run_pytest(python: str, cwd: Path, timeout_s: int = 120) -> tuple[int | None, str, Path | None]:
    """Run the task suite once; return (exit, log, junit path or None)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="lc-val-"))
    report = tmpdir / "report.xml"
    argv = [python, "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider",
            "--junitxml", str(report), "--rootdir", str(cwd)]
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return None, "<timeout>", None
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), report if report.exists() else None


def _apply_unified(repo: Path, diff_text: str, focus: set[str]) -> None:
    """Apply a gold/wrong diff to a scratch copy via the product applier."""
    from .workspace import CandidateWorkspace

    ws = CandidateWorkspace.__new__(CandidateWorkspace)
    ws.root = repo
    ws.apply_diff(diff_text, focus, ["src/**/*.py"], 32 * 1024)


def validate_task(suite_path: Path, entry: dict, python: str) -> dict:
    """Offline validation: broken fails, gold passes, controls fail."""
    from .execution.pytest_parser import parse_junit

    paths = task_paths(suite_path, entry)
    result: dict = {"id": entry["id"], "frozen": [], "gold_hidden": [],
                    "broken": None, "gold": None, "null": None, "wrong": None}
    result["frozen"] = verify_frozen(suite_path, entry)
    result["gold_hidden"] = gold_hidden_from_repo(suite_path, entry)
    if result["frozen"] or result["gold_hidden"]:
        return result
    with tempfile.TemporaryDirectory(prefix="lc-valrepo-") as tmp:
        scratch = Path(tmp) / "repo"
        shutil.copytree(paths["repo"], scratch,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".local-coder"))
        code, _, report = _run_pytest(python, scratch)
        broken_ids = []
        if report is not None:
            try:
                broken_ids = parse_junit(report).failed + parse_junit(report).errors
            except ValueError:
                broken_ids = []
        result["broken"] = {"exit": code, "failed": broken_ids}
        result["null"] = {"still_failing": bool(broken_ids)}
        from .paths import build_inventory

        focus = {f for f in build_inventory(scratch) if f.startswith("src/")}
        _apply_unified(scratch, paths["gold"].read_text(encoding="utf-8"), focus)
        code, _, report = _run_pytest(python, scratch)
        passed_all = False
        if report is not None:
            try:
                parsed = parse_junit(report)
                passed_all = not parsed.failed and not parsed.errors and bool(parsed.collected)
            except ValueError:
                passed_all = False
        result["gold"] = {"exit": code, "all_pass": passed_all}
        if paths["wrong"] is not None and paths["wrong"].exists():
            shutil.rmtree(scratch)
            shutil.copytree(paths["repo"], scratch,
                            ignore=shutil.ignore_patterns(".git", "__pycache__", ".local-coder"))
            _apply_unified(scratch, paths["wrong"].read_text(encoding="utf-8"), focus)
            code, _, report = _run_pytest(python, scratch)
            still_bad = True
            if report is not None:
                try:
                    parsed = parse_junit(report)
                    still_bad = bool(parsed.failed or parsed.errors)
                except ValueError:
                    still_bad = True
            result["wrong"] = {"exit": code, "still_failing": still_bad}
    return result


def validate_suite(suite_path: Path, python: str) -> dict:
    """Validate every task. Returns per-task results plus a verdict."""
    manifest = load_manifest(suite_path)
    results = [validate_task(suite_path, entry, python) for entry in manifest["entries"]]
    bad = [r["id"] for r in results
           if r["frozen"] or r["gold_hidden"] or not (r["broken"] or {}).get("failed")
           or not (r["gold"] or {}).get("all_pass")
           or not (r["null"] or {}).get("still_failing")
           or (r["wrong"] is not None and not r["wrong"].get("still_failing"))]
    return {"suite": manifest["name"], "suite_version": manifest["suite_version"],
            "tasks": results, "invalid": bad, "valid": not bad}


# -- live runs ------------------------------------------------------------


def host_available_mb() -> float | None:
    """Best-effort host available memory, None when unmeasurable."""
    from .telemetry import host_memory_mb

    return host_memory_mb().get("available_mb")


def failure_category(outcome: str, stop_reason: str | None) -> str:
    """Normalize terminal outcomes into reportable buckets."""
    reason = (stop_reason or "").lower()
    if outcome == "solved":
        return "solved"
    if outcome == "setup_failed":
        return "setup-failed"
    if "budget exhausted" in reason or "ninth" in reason:
        return "blocked-budget"
    if "invalid" in reason and "output" in reason:
        return "blocked-invalid-output"
    if "no-progress" in reason or "repetition" in reason:
        return "blocked-no-progress"
    if "validation" in reason or "not_solved" in reason or "not solved" in reason:
        return "blocked-validation-failed"
    if "elapsed" in reason:
        return "blocked-elapsed"
    return "blocked-other"


def sanitize(value, home: str):
    """Strip absolute host paths for committable reports."""
    if isinstance(value, str):
        return value.replace(home, "~")
    if isinstance(value, list):
        return [sanitize(v, home) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, home) for k, v in value.items()}
    return value


def run_suite(suite_path: Path, profile: str, adapter_factory, backend_factory,
              state_dir: Path, run_id: str, splits=("dev", "held-out"),
              max_tickets: int = 20, report_dir: Path | None = None) -> dict:
    """One sequential bounded pass over frozen tasks. First outcomes kept."""
    from .config import load_config
    from .controller import Controller
    from .execution.isolated import IsolationUnavailable
    from .export import SKILL_ACCEPTED
    from .persistence import EpisodeStore

    manifest = load_manifest(suite_path)
    try:
        backend_probe = backend_factory()
        backend_probe.check_available()
        backend_available = True
    except (IsolationUnavailable, Exception) as exc:  # noqa: BLE001 - recorded, not raised
        backend_available = False
        backend_error = str(exc)
    store = EpisodeStore(state_dir)
    home = os.path.expanduser("~")
    report: dict = {
        "report_version": REPORT_VERSION, "run_id": run_id, "suite": manifest["name"],
        "profile": profile, "started": utc_now(), "splits": list(splits),
        "backend_available": backend_available,
        "tasks": [], "stopped_early": None,
    }
    if not backend_available:
        report["backend_error"] = backend_error
    scheduled = [e for e in manifest["entries"] if e["split"] in splits][:max_tickets]
    report["scheduled"] = [e["id"] for e in scheduled]
    attempted = 0
    for entry in scheduled:
        available = host_available_mb()
        if available is not None and available < MIN_HOST_AVAILABLE_MB:
            report["stopped_early"] = (
                f"sustained resource pressure: host available {available}MB "
                f"below {MIN_HOST_AVAILABLE_MB}MB floor")
            break
        drift = verify_frozen(suite_path, entry)
        paths = task_paths(suite_path, entry)
        ticket = paths["ticket"].read_text(encoding="utf-8")
        task_report: dict = {"id": entry["id"], "family": entry["family"],
                             "split": entry["split"], "origin": entry["origin"],
                             "status": "scheduled"}
        if drift:
            task_report.update({"status": "setup-failed", "detail": f"freeze drift: {drift}"})
            report["tasks"].append(task_report)
            continue
        attempted += 1
        episode_id = f"{run_id}-{entry['id']}"
        started = time.monotonic()
        try:
            config = load_config(paths["repo"])
            adapter = adapter_factory(entry)
            backend = backend_factory()
            controller = Controller(store, config, adapter, backend, paths["repo"], ticket,
                                    episode_id=episode_id, task_id=entry["id"])
            state = controller.run()
        except (IsolationUnavailable, RuntimeError) as exc:
            task_report.update({"status": "setup-failed", "detail": str(exc)[:300]})
            report["tasks"].append(task_report)
            continue
        elapsed = round(time.monotonic() - started, 1)
        events = store.read_events(episode_id)
        observations = [e for e in events if e["type"] == "observation"]
        accepted = sum(1 for e in observations if e["data"].get("status") in SKILL_ACCEPTED)
        rejected = sum(1 for e in observations
                       if e["data"].get("status") in ("rejected", "protocol_error", "no_progress"))
        regressions = [e for e in events if e["type"] == "check_completed"
                       and e["data"].get("regression") == "True"]
        recovered = any(e["type"] == "patch_reverted" for e in events)
        focus = [f.path for f in state.focus] if state.focus else []
        labels = json.loads(paths["labels"].read_text(encoding="utf-8"))
        gold_files = labels.get("files", [])
        recall_1 = (1.0 if gold_files and any(f == gold_files[0] for f in focus[:1]) else 0.0) \
            if gold_files else None
        recall_3 = (len([f for f in gold_files if f in focus[:3]]) / len(gold_files)) \
            if gold_files else None
        handoff_ok = None
        if state.outcome == "blocked":
            try:
                handoff = (store.episode_dir(episode_id) / "handoff.md").read_text(encoding="utf-8")
                handoff_ok = all(s in handoff for s in (
                    "## Goal", "## Relevant files", "## Attempted changes",
                    "## Latest failure", "## Why stopping"))
            except OSError:
                handoff_ok = False
        mem = {}
        for e in events:
            if e["type"] == "handoff_written" and isinstance(e["data"].get("memory"), dict):
                mem = e["data"]["memory"]
        task_report.update({
            "status": state.outcome,
            "category": failure_category(state.outcome, state.stop_reason),
            "stop_reason": state.stop_reason,
            "generations": state.generations_used,
            "patch_attempts": state.patch_attempts,
            "model_checks": state.model_checks,
            "test_invocations": state.test_invocations,
            "prompt_tokens_exact": state.prompt_tokens_exact,
            "completion_tokens_exact": state.completion_tokens_exact,
            "prompt_tokens_est": state.prompt_tokens_est,
            "completion_tokens_est": state.completion_tokens_est,
            "elapsed_s": elapsed,
            "episode_elapsed_s": round(state.elapsed_s, 1),
            "memory": mem,
            "actions_valid": sum(1 for e in events if e["type"] == "model_response"),
            "protocol_errors": sum(1 for e in events if e["type"] == "protocol_error"),
            "skill_accepted": accepted,
            "skill_rejected": rejected,
            "patch_applied": any(e["type"] == "patch_applied" for e in events),
            "regression": bool(regressions),
            "recovery": recovered,
            "handoff_ok": handoff_ok,
            "recall_at_1": recall_1,
            "recall_at_3": recall_3,
            "human_interventions": 0,
        })
        report["tasks"].append(task_report)
    report["finished"] = utc_now()
    report["attempted"] = attempted
    if report_dir is not None:
        write_reports(report, report_dir, home)
    return report


def aggregate(report: dict) -> dict:
    """Aggregate metrics with explicit denominators (nothing dropped)."""
    tasks = report["tasks"]
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for task in tasks:
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
        by_category[task.get("category", "unknown")] = by_category.get(task.get("category", "unknown"), 0) + 1
    completed = [t for t in tasks if t["status"] in ("solved", "blocked")]
    solved = [t for t in completed if t["status"] == "solved"]
    attempted_outcomes = [t for t in tasks if t["status"] != "scheduled"]
    recalls_1 = [t["recall_at_1"] for t in completed if t.get("recall_at_1") is not None]
    recalls_3 = [t["recall_at_3"] for t in completed if t.get("recall_at_3") is not None]
    gens = [t["generations"] for t in completed if isinstance(t.get("generations"), int)]
    return {
        "scheduled": len(tasks),
        "attempted": len(attempted_outcomes),
        "completed": len(completed),
        "by_status": by_status,
        "by_category": by_category,
        "solved": len(solved),
        "solve_rate_completed": round(len(solved) / len(completed), 3) if completed else None,
        "solve_rate_attempted": round(len(solved) / len(attempted_outcomes), 3) if attempted_outcomes else None,
        "mean_generations_completed": round(sum(gens) / len(gens), 2) if gens else None,
        "mean_recall_at_1": round(sum(recalls_1) / len(recalls_1), 3) if recalls_1 else None,
        "mean_recall_at_3": round(sum(recalls_3) / len(recalls_3), 3) if recalls_3 else None,
        "with_regression": sum(1 for t in completed if t.get("regression")),
        "with_recovery": sum(1 for t in completed if t.get("recovery")),
        "handoff_ok_blocked": sum(1 for t in completed
                                  if t["status"] == "blocked" and t.get("handoff_ok")),
        "blocked_total": sum(1 for t in completed if t["status"] == "blocked"),
    }


CSV_FIELDS = ["id", "family", "split", "origin", "status", "category", "generations",
              "patch_attempts", "model_checks", "test_invocations", "prompt_tokens_exact",
              "completion_tokens_exact", "elapsed_s", "protocol_errors", "skill_accepted",
              "skill_rejected", "patch_applied", "regression", "recovery", "handoff_ok",
              "recall_at_1", "recall_at_3"]


def write_reports(report: dict, report_dir: Path, home: str) -> dict[str, Path]:
    """Write sanitized JSON, CSV, and Markdown reports. Returns paths."""
    report_dir.mkdir(parents=True, exist_ok=True)
    clean = sanitize(report, home)
    paths = {}
    paths["json"] = report_dir / "report.json"
    paths["json"].write_text(json.dumps({**clean, "aggregate": aggregate(report)},
                                        indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["csv"] = report_dir / "report.csv"
    with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for task in clean["tasks"]:
            writer.writerow(task)
    paths["md"] = report_dir / "report.md"
    paths["md"].write_text(render_markdown(clean, aggregate(report)), encoding="utf-8")
    return paths


def render_markdown(report: dict, agg: dict) -> str:
    """Human-readable pilot summary with explicit denominators."""
    lines = [
        f"# Pilot report {report['run_id']}",
        "",
        f"Suite: {report['suite']} | profile: {report['profile']} | "
        f"splits: {', '.join(report['splits'])}",
        f"Started: {report.get('started', '?')} | finished: {report.get('finished', '?')}",
        f"Backend available: {report.get('backend_available')}",
        "",
        "## Aggregate (nothing dropped silently)",
        "",
        f"- Scheduled: {agg['scheduled']} | attempted: {agg['attempted']} | "
        f"completed: {agg['completed']}",
        f"- Solved: {agg['solved']} "
        f"(of completed: {agg['solve_rate_completed']}; of attempted: {agg['solve_rate_attempted']})",
        f"- By status: {agg['by_status']}",
        f"- By category: {agg['by_category']}",
        f"- Mean generations (completed): {agg['mean_generations_completed']}",
        f"- Mean recall@1/@3 (gold labels only): {agg['mean_recall_at_1']}/{agg['mean_recall_at_3']}",
        f"- Episodes with regression: {agg['with_regression']} | "
        f"with recovery: {agg['with_recovery']}",
        f"- Blocked with useful handoff: {agg['handoff_ok_blocked']}/{agg['blocked_total']}",
        "",
        "## Per-ticket outcomes",
        "",
        "| ticket | split | status | category | gens | patch? | recall@1 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in report["tasks"]:
        lines.append(
            f"| {task['id']} | {task.get('split', '?')} | {task['status']} | "
            f"{task.get('category', '?')} | {task.get('generations', '-')} | "
            f"{task.get('patch_applied', '-')} | {task.get('recall_at_1', '-')} |")
    if report.get("stopped_early"):
        lines += ["", f"Stopped early: {report['stopped_early']}"]
    lines += ["",
              "_Generated fixtures test engineering correctness, not representative "
              "real-world coding performance._",
              ""]
    return "\n".join(lines)
