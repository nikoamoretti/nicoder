"""CHECK: real tests, trusted commands, structured evidence.

The model selects a registered scope, never a command. JUnit identities are
compared against the fixed baseline; a smaller failure count with different
test IDs is not progress. Zero tests, missing tests, new skips, corrupt
reports, and early exits are incomplete, never a pass.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ..execution.base import ExecutionBackend
from ..execution.pytest_parser import (
    ParsedSuite,
    classify_exit_code,
    clip_text,
    parse_junit,
    summarize_log_failure,
)
from ..state import CheckEvidence, EpisodeState
from ..workspace import CandidateWorkspace


def _collect_argv(base: list[str], report_path: Path, rootdir: Path) -> list[str]:
    # Controller-owned machine flags only: the model never influences argv.
    # --rootdir pins test-ID generation to the candidate tree so IDs do not
    # depend on where the episode store lives on the host filesystem.
    return list(base) + ["--junitxml", str(report_path), "--rootdir", str(rootdir)]


def execute_scope(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    backend: ExecutionBackend,
    scope: str,
    command: list[str],
    timeout_s: int,
    max_log_bytes: int,
    log_name: str,
    write_artifact_text,
    write_artifact_bytes,
) -> CheckEvidence:
    """Run one registered scope and parse its structured evidence.

    Source mutations left behind by the test run are detected from trusted
    manifests (not from anything the candidate claims): any non-byproduct
    change invalidates the evidence, because the tested tree is no longer
    the tree the episode reasons about.
    """
    before = workspace.manifest()
    tree = workspace.tree_hash()
    # Absolute candidate path: pytest --rootdir and container mounts must not
    # depend on the supervisor's working directory. Hashes are content-based
    # and unaffected by this.
    cwd = workspace.root.resolve()
    with tempfile.TemporaryDirectory(prefix="lc-report-") as tmpdir:
        report_path = Path(tmpdir) / "report.xml"
        argv = _collect_argv(command, report_path, cwd)
        result = backend.run(argv, cwd, timeout_s, max_log_bytes)
        junit_bytes: bytes | None = None
        if report_path.exists():
            junit_bytes = report_path.read_bytes()
    log_ref = write_artifact_text(state.episode_id, log_name, result.log_text)
    report_ref = None
    parsed: ParsedSuite | None = None
    corrupt: str | None = None
    if junit_bytes is not None:
        tmp_report = Path(tempfile.mkdtemp(prefix="lc-junit-")) / "report.xml"
        tmp_report.write_bytes(junit_bytes)
        try:
            parsed = parse_junit(tmp_report)
            report_ref = write_artifact_bytes(state.episode_id, log_name.replace(".log", ".xml"), junit_bytes)
        except ValueError as exc:
            corrupt = str(exc)
    evidence = evaluate(
        scope=scope,
        argv=argv,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_s=result.duration_s,
        log_text=result.log_text,
        truncated=result.truncated,
        parsed=parsed,
        corrupt=corrupt,
        tree_hash=tree,
        log_ref=log_ref,
        report_ref=report_ref,
        required=state.required_tests,
        baseline_skipped=set(state.baseline.skipped) if state.baseline else set(),
        acceptance_allowed=set(state.acceptance.preexisting_failures),
    )
    after = workspace.manifest()
    mutated = sorted(
        rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)
    )
    if mutated:
        evidence.mutations = mutated
        if evidence.status != "incomplete":
            evidence.status = "incomplete"
            evidence.incomplete_reason = (
                f"candidate mutated during test run (unaccounted source change): "
                f"{mutated[:5]}"
            )
    return evidence


def evaluate(
    scope: str,
    argv: list[str],
    exit_code: int | None,
    timed_out: bool,
    duration_s: float,
    log_text: str,
    truncated: bool,
    parsed: ParsedSuite | None,
    corrupt: str | None,
    tree_hash: str,
    log_ref: str,
    report_ref: str | None,
    required: list[str],
    baseline_skipped: set[str],
    acceptance_allowed: set[str],
) -> CheckEvidence:
    """Decide pass/fail/incomplete from owned evidence. Pure function.

    The process exit code is authoritative about the runner; the JUnit
    report is authoritative about test identities. Both must agree:
    interrupted, internal-error, usage-error, and no-test exits are
    incomplete even when a parseable report exists, and an ok/failed exit
    paired with a contradicting report is inconsistent, never a pass.
    The pre-existing-failure policy applies only to a completed,
    consistent run — an allowed failure is not a broken runner.
    """
    base = CheckEvidence(
        scope=scope, argv=argv, exit_code=exit_code, status="incomplete",
        duration_s=round(duration_s, 3), log_artifact=log_ref,
        report_artifact=report_ref, tree_hash=tree_hash, truncated=truncated,
    )
    if timed_out or exit_code is None:
        base.incomplete_reason = "timeout" if timed_out else "process did not exit"
        return base
    if corrupt is not None or parsed is None:
        base.incomplete_reason = f"corrupt test report: {corrupt or 'missing junit xml'}"
        return base
    # Report contents are facts about the run and are attached even when a
    # later gate rules the evidence incomplete; only the status claims.
    base.passed = parsed.passed
    base.failed = parsed.failed
    base.errors = parsed.errors
    base.skipped = parsed.skipped
    base.collected = parsed.collected
    base.collection_error = parsed.collection_error
    outcome = classify_exit_code(exit_code)
    if outcome == "interrupted":
        base.incomplete_reason = f"pytest interrupted (exit {exit_code}); evidence unusable"
        return base
    if outcome == "internal_error":
        base.incomplete_reason = (
            f"pytest internal error (exit {exit_code}); report content is not evidence"
        )
        return base
    if outcome == "usage_error":
        base.incomplete_reason = f"pytest usage error (exit {exit_code}); runner misconfigured"
        return base
    if outcome == "no_tests":
        base.incomplete_reason = f"no tests collected (exit {exit_code})"
        return base
    if outcome == "unknown":
        base.incomplete_reason = f"unknown pytest exit code {exit_code}; refusing to interpret"
        return base
    if outcome == "ok" and (parsed.failed or parsed.errors):
        base.incomplete_reason = (
            f"exit 0 contradicts report ({len(parsed.failed)} failed, "
            f"{len(parsed.errors)} errors); refusing to interpret"
        )
        return base
    if outcome == "tests_failed" and not (parsed.failed or parsed.errors):
        base.incomplete_reason = "exit 1 but report shows no failures; refusing to interpret"
        return base
    if not parsed.collected:
        base.incomplete_reason = "zero tests collected"
        return base
    if parsed.collection_error:
        base.incomplete_reason = f"collection error: {clip_text(parsed.collection_error, 200)}"
        return base
    missing = [t for t in required if t not in parsed.collected]
    if missing:
        base.incomplete_reason = f"required tests no longer collected: {missing[:5]}"
        return base
    newly_skipped = [t for t in parsed.skipped if t in required and t not in baseline_skipped]
    if newly_skipped and scope != "baseline":
        base.incomplete_reason = f"required tests newly skipped: {newly_skipped[:5]}"
        return base
    summary, frames = summarize_log_failure(log_text, parsed.failed + parsed.errors)
    base.failure_summary = summary
    base.relevant_frames = [f for f in frames if f][:8]
    bad = [t for t in (parsed.failed + parsed.errors) if t not in acceptance_allowed]
    base.status = "pass" if not bad else "fail"
    return base


def compare_to_baseline(baseline: CheckEvidence, current: CheckEvidence) -> dict:
    """Identity-based progress from one shared implementation.

    A previously failing test counts as fixed ONLY when valid current
    evidence explicitly shows it passing. Skipped, missing, or simply not
    executed tests are reported in their own categories, never as fixes.
    Incomplete evidence on either side makes the pair incomparable: no
    fixed/still-failing/new/regression claims are produced at all, so the
    briefing and recorded observations cannot disagree with each other.
    """
    base_bad = set(baseline.failed) | set(baseline.errors)
    cur_bad = set(current.failed) | set(current.errors)
    comparable = baseline.status in ("pass", "fail") and current.status in ("pass", "fail")
    unresolved = sorted(base_bad - set(current.passed) - cur_bad)
    skipped_now = sorted(set(unresolved) & set(current.skipped))
    missing = sorted(set(unresolved) - set(current.collected))
    if not comparable:
        return {
            "comparable": False,
            "baseline_failing": sorted(base_bad),
            "current_failing": sorted(cur_bad),
            "fixed": [],
            "still_failing": [],
            "new_failures": [],
            "regression": False,
            "regressed_tests": [],
            "unresolved": unresolved,
            "skipped_now": skipped_now,
            "missing": missing,
        }
    fixed = sorted(base_bad & set(current.passed))
    still_failing = sorted(base_bad & cur_bad)
    new = sorted(cur_bad - base_bad)
    regressed = sorted(set(baseline.passed) & cur_bad)
    return {
        "comparable": True,
        "baseline_failing": sorted(base_bad),
        "current_failing": sorted(cur_bad),
        "fixed": fixed,
        "still_failing": still_failing,
        "new_failures": new,
        "regression": bool(regressed),
        "regressed_tests": regressed,
        "unresolved": unresolved,
        "skipped_now": skipped_now,
        "missing": missing,
    }
