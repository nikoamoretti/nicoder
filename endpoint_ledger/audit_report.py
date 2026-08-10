"""Run audits: evidence-backed classification of every failed scored check.

Produces a human-readable report plus persisted ScoreAdjudication records.
The original Score rows are never modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import Observation, Probe, ProbeVersion, Score, SuiteRun
from endpoint_ledger.measurement.adjudication import (
    default_failure_classification,
    persist_adjudications,
)
from endpoint_ledger.measurement.cohorts import observation_baseline_eligible
from endpoint_ledger.measurement.roles import role_for_metric

REPORT_PATH_TEMPLATE = "docs/audits/run-{run_id}-audit.md"


@dataclass
class FailedCheck:
    suite_run_id: int
    observation_id: int
    probe_stable_key: str
    probe_version_number: int
    repeat_index: int
    scorer_name: str
    scorer_version: str
    metric_name: str
    score_role: str
    expected_behavior: dict[str, Any]
    normalized_text: str | None
    normalized_tool_calls: list[dict[str, Any]] | None
    passed: bool
    explanation: str
    details: dict[str, Any]
    raw_response_hash: str | None
    raw_evidence_exists: bool
    classification: str
    classification_notes: str
    baseline_eligible: bool
    eligibility_reason: str


def collect_failed_checks(
    session: Session,
    run_id: int,
    settings: Settings | None = None,
) -> list[FailedCheck]:
    settings = settings or get_settings()
    rows = session.execute(
        select(Score, Observation, ProbeVersion, Probe, SuiteRun)
        .join(Observation, Observation.id == Score.observation_id)
        .join(ProbeVersion, ProbeVersion.id == Observation.probe_version_id)
        .join(Probe, Probe.id == ProbeVersion.probe_id)
        .join(SuiteRun, SuiteRun.id == Observation.suite_run_id)
        .where(Observation.suite_run_id == run_id, Score.passed.is_(False))
        .order_by(Probe.stable_key, Observation.repeat_index, Score.scorer_name)
    ).all()

    checks: list[FailedCheck] = []
    for score, observation, probe_version, probe, _suite in rows:
        classification, notes = default_failure_classification(score, observation, probe_version)
        eligible, reason = observation_baseline_eligible(observation)
        checks.append(
            FailedCheck(
                suite_run_id=run_id,
                observation_id=observation.id,
                probe_stable_key=probe.stable_key,
                probe_version_number=probe_version.version_number,
                repeat_index=observation.repeat_index,
                scorer_name=score.scorer_name,
                scorer_version=score.scorer_version,
                metric_name=score.metric_name,
                score_role=score.role or role_for_metric(score.metric_name, probe_version.primary_metric),
                expected_behavior=probe_version.expected_behavior,
                normalized_text=observation.normalized_text,
                normalized_tool_calls=observation.normalized_tool_calls,
                passed=bool(score.passed),
                explanation=score.explanation,
                details=score.details or {},
                raw_response_hash=observation.raw_response_hash,
                raw_evidence_exists=observation.raw_response is not None,
                classification=classification,
                classification_notes=notes,
                baseline_eligible=eligible,
                eligibility_reason=reason,
            )
        )
    return checks


def audit_run(
    session: Session,
    run_id: int,
    settings: Settings | None = None,
    reviewer_label: str = "audit-cli",
) -> list[FailedCheck]:
    """Persist adjudications for every failed check of a run and return them."""
    settings = settings or get_settings()
    run = session.get(SuiteRun, run_id)
    if run is None:
        raise ValueError(f"suite run {run_id} does not exist")
    checks = collect_failed_checks(session, run_id, settings)

    adjudications = []
    for check in checks:
        score = session.scalar(
            select(Score).where(
                Score.observation_id == check.observation_id,
                Score.metric_name == check.metric_name,
                Score.scorer_name == check.scorer_name,
            )
        )
        if score is not None:
            adjudications.append((score, check.classification, check.classification_notes))
    persist_adjudications(session, adjudications, reviewer_label=reviewer_label)
    return checks


def render_report(run_id: int, checks: list[FailedCheck], run: SuiteRun) -> str:
    """Markdown report explaining every failed check and baseline eligibility."""
    lines: list[str] = []
    lines.append(f"# Audit — Suite Run {run_id}")
    lines.append("")
    lines.append(f"- Suite: `{run.suite_name}`")
    lines.append(f"- Status: `{run.status}`")
    lines.append(f"- Data origin: `{run.data_origin}`")
    lines.append(
        f"- Attempted: {run.total_expected_observations} · Completed: "
        f"{run.completed_observations} · Failed: {run.failed_observations}"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"Failed scored checks: **{len(checks)}**")
    lines.append("")
    from collections import Counter

    by_classification = Counter(c.classification for c in checks)
    for classification, count in sorted(by_classification.items()):
        lines.append(f"- `{classification}`: {count}")
    lines.append("")
    lines.append("## Important")
    lines.append("")
    lines.append(
        "A failed scored check is evidence that a measurement did not pass. "
        "It is **not** automatically evidence of model behavior. Each check below "
        "carries a preliminary classification and an explicit baseline-eligibility "
        "statement."
    )
    lines.append("")

    for index, check in enumerate(checks, start=1):
        lines.append(f"## Failed check {index} — `{check.metric_name}`")
        lines.append("")
        lines.append(f"- Suite run ID: {check.suite_run_id}")
        lines.append(f"- Observation ID: {check.observation_id}")
        lines.append(f"- Probe: `{check.probe_stable_key}` v{check.probe_version_number}")
        lines.append(f"- Repeat index: {check.repeat_index}")
        lines.append(f"- Scorer: `{check.scorer_name}` v{check.scorer_version}")
        lines.append(f"- Score role: `{check.score_role}`")
        lines.append("- Result: **fail**")
        lines.append(f"- Explanation: {check.explanation}")
        lines.append(f"- Raw response hash: `{check.raw_response_hash or '—'}`")
        lines.append(f"- Raw evidence stored: {'yes' if check.raw_evidence_exists else 'no'}")
        lines.append(f"- Preliminary classification: `{check.classification}`")
        lines.append(f"- Classification notes: {check.classification_notes}")
        lines.append("")
        if check.normalized_text:
            lines.append("Normalized observed behavior:")
            lines.append("")
            lines.append(f"```json\n{check.normalized_text}\n```")
            lines.append("")
        if check.normalized_tool_calls:
            lines.append("Observed tool calls:")
            lines.append("")
            lines.append(f"```json\n{_json_dumps(check.normalized_tool_calls)}\n```")
            lines.append("")
        lines.append("Baseline eligibility:")
        lines.append("")
        if check.baseline_eligible:
            lines.append("- **Eligible** for a live behavioral baseline.")
        else:
            lines.append("- **NOT eligible** for a live behavioral baseline.")
            lines.append(f"- Reason: {check.eligibility_reason}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"_Generated by `audit-run` at {datetime.now(UTC).isoformat()}. "
        "Adjudications are separate records; the original Score rows were not modified._"
    )
    return "\n".join(lines)


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2)
