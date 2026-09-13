"""Blocked/solved handoff: goal, files, hypothesis, attempts, failure, reason."""
from __future__ import annotations

from .state import EpisodeState


def render_handoff(state: EpisodeState, changed_files: list[str], evidence_dir: str) -> str:
    """Render handoff.md so the next human or run can continue usefully."""
    lines = [
        f"# Handoff {state.episode_id}",
        "",
        f"Outcome: {state.outcome}",
        f"Stop reason: {state.stop_reason or 'n/a'}",
        f"Phase: {state.phase}",
        f"Generations used: {state.generations_used}/{state.budgets.max_generations}",
        f"Patches: {state.patch_attempts}, model checks: {state.model_checks}, "
        f"test invocations: {state.test_invocations}",
        "",
        "## Goal",
        state.ticket.strip(),
        "",
        "## Relevant files",
    ]
    if state.focus:
        for foc in state.focus:
            lines.append(f"- {foc.path}:{foc.start}-{foc.end} ({foc.reason})")
    else:
        lines.append("- (no focus files selected)")
    lines += ["", "## Hypothesis (proposed, not a fact)", state.hypothesis or "(none recorded)", "",
              "## Attempted changes"]
    if state.patch_history or state.last_patch:
        for record in ([*state.patch_history, state.last_patch] if state.last_patch else state.patch_history):
            lines.append(f"- {record.sha}: {record.summary} [{', '.join(record.files)}]")
    else:
        lines.append("- (no patches applied)")
    if changed_files:
        lines.append(f"Changed vs original: {', '.join(changed_files)}")
    lines += ["", "## Latest failure"]
    current = state.final or state.current
    if current is None:
        lines.append("(no CHECK evidence recorded)")
    elif current.status == "pass":
        lines.append(f"All required checks passing ({current.scope}, {len(current.passed)} passed).")
    else:
        lines.append(f"Scope: {current.scope}, status: {current.status}, exit: {current.exit_code}")
        if current.failure_summary:
            lines.append(f"Failure: {current.failure_summary}")
        if current.relevant_frames:
            lines.append("Frames: " + ", ".join(current.relevant_frames[:8]))
        if current.failed:
            lines.append("Failed: " + ", ".join(current.failed[:10]))
        if current.incomplete_reason:
            lines.append(f"Incomplete: {current.incomplete_reason}")
    lines += ["", "## Why stopping", state.stop_reason or "(running)", "",
              "## Evidence", f"Episode directory: {evidence_dir}",
              "See state.json (canonical state), events.jsonl (trajectory), briefing.txt,",
              "and artifacts/ (logs, diffs, search results).", ""]
    return "\n".join(lines)
