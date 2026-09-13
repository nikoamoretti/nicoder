"""Briefing generation: rendered afresh from canonical state every turn.

The prompt never grows by appending conversation. Raw logs stay in
artifacts; the model sees compact structured observations and selected
source excerpts with line numbers and content identity. Model hypotheses
are labeled as proposals, never facts. Source comments and logs are
untrusted task data, not controller instructions.
"""
from __future__ import annotations

from .protocol import Action, estimate_tokens
from .state import EpisodeState
from .workspace import CandidateWorkspace

ACTION_SCHEMA = """\
Choose exactly ONE action as a single JSON object (no fences, no list):
{"action":"find","query":"...","symbols":["Name"]} |
{"action":"patch","operation":"apply","diff":"<unified diff>","expected_effect":"..."} |
{"action":"patch","operation":"revert_last","reason_code":"..."} |
{"action":"check","scope":"focused|regression"} |
{"action":"done","status":"solved","summary":"..."} |
{"action":"done","status":"blocked","reason_code":"...","summary":"..."}
PATCH may touch only current focus files. CHECK runs a registered scope."""


class ContextBudgetExceeded(Exception):
    """Even the minimum valid prompt cannot fit the token budget."""


def render(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    last_observation: dict | None,
    omitted_note: list[str] | None = None,
) -> str:
    """Render the full briefing for the next generation."""
    excerpts, omitted = _collect_excerpts(state, workspace)
    sections = [
        f"TASK\n{state.ticket.strip()}",
        _budget_section(state),
        "CURRENT HYPOTHESIS (model proposal, not a fact)\n" + (state.hypothesis or "(none)"),
        _focus_section(state, excerpts, omitted),
        _last_change_section(state),
        _check_section(state),
        _progress_section(state),
        _observation_section(last_observation),
        "RULES\n" + ACTION_SCHEMA,
    ]
    text = "\n\n".join(sections) + "\n"
    tokens = estimate_tokens(text)
    if tokens > state.budgets.max_prompt_tokens:
        raise ContextBudgetExceeded(
            f"minimum briefing is ~{tokens} tokens, budget is {state.budgets.max_prompt_tokens}"
        )
    if omitted_note is not None:
        omitted_note.extend(omitted)
    return text


def _budget_section(state: EpisodeState) -> str:
    b = state.budgets
    return (
        "BUDGET\n"
        f"step {state.generations_used}/{b.max_generations} generations; "
        f"patch attempts {state.patch_attempts}/{b.max_patch_attempts}; "
        f"model checks {state.model_checks}/{b.max_model_checks}; "
        f"test invocations {state.test_invocations}/{b.max_test_invocations}; "
        f"phase {state.phase}"
    )


def _collect_excerpts(
    state: EpisodeState, workspace: CandidateWorkspace
) -> tuple[list[tuple[str, str]], list[str]]:
    """Read current focus ranges. Lowest-priority entries trim first."""
    excerpts: list[tuple[str, str]] = []
    omitted: list[str] = []
    budget_chars = state.budgets.briefing_target_tokens * 4
    used = 0
    for foc in state.focus:
        try:
            text = workspace.read_range(foc.path, foc.start, foc.end)
        except Exception as exc:
            omitted.append(f"{foc.path} (unreadable: {exc})")
            continue
        block = f"--- {foc.path}:{foc.start}-{foc.end} sha={foc.sha256[:12]} ---\n{text}"
        if used + len(block) > budget_chars and excerpts:
            omitted.append(f"{foc.path}:{foc.start}-{foc.end} (trimmed by briefing budget)")
            continue
        used += len(block)
        excerpts.append((foc.path, block))
    return excerpts, omitted


def _focus_section(state: EpisodeState, excerpts: list[tuple[str, str]], omitted: list[str]) -> str:
    if not state.focus:
        return "FILES\n(no focus files yet; use FIND first)"
    lines = ["FILES"]
    by_path = dict(excerpts)
    for foc in state.focus:
        lines.append(f"{foc.path}:{foc.start}-{foc.end} sha={foc.sha256[:12]} — {foc.reason}")
        if foc.path in by_path:
            lines.append(by_path[foc.path])
    for note in omitted:
        lines.append(f"[omitted excerpt: {note}]")
    return "\n".join(lines)


def _last_change_section(state: EpisodeState) -> str:
    if state.last_patch is None:
        return "LAST CHANGE\n(none)"
    return (
        "LAST CHANGE\n"
        f"patch {state.last_patch.sha}: {state.last_patch.summary} "
        f"[{', '.join(state.last_patch.files)}]"
    )


def _check_section(state: EpisodeState) -> str:
    current = state.current
    if current is None:
        base = state.baseline
        if base is None:
            return "CHECK\n(no baseline yet)"
        return (
            "CHECK (baseline)\n"
            f"status={base.status} failed={base.failed} errors={base.errors} "
            f"passed={len(base.passed)} collected={len(base.collected)}"
        )
    lines = [
        "CHECK (latest)",
        f"scope={current.scope} status={current.status} exit={current.exit_code}",
        f"failed={current.failed or []} errors={current.errors or []} skipped={current.skipped or []}",
    ]
    if current.failure_summary:
        lines.append(f"failure: {current.failure_summary}")
    if current.relevant_frames:
        lines.append("frames: " + ", ".join(current.relevant_frames[:5]))
    if current.incomplete_reason:
        lines.append(f"incomplete: {current.incomplete_reason}")
    if current.mutations:
        lines.append(f"mutated during run: {current.mutations[:5]}")
    return "\n".join(lines)


def _progress_section(state: EpisodeState) -> str:
    if state.baseline is None or state.current is None:
        return "PROGRESS\n(no comparison yet)"
    base_bad = set(state.baseline.failed) | set(state.baseline.errors)
    cur_bad = set(state.current.failed) | set(state.current.errors)
    fixed = sorted(base_bad - cur_bad)
    new = sorted(cur_bad - base_bad)
    return (
        "PROGRESS (test identities, not counts)\n"
        f"baseline failing: {sorted(base_bad) or []}\n"
        f"current failing: {sorted(cur_bad) or []}\n"
        f"fixed: {fixed or []} new: {new or []} regression={state.regression}"
    )


def _observation_section(observation: dict | None) -> str:
    if not observation:
        return "LAST OBSERVATION\n(none)"
    import json as _json

    return "LAST OBSERVATION\n" + _json.dumps(observation, sort_keys=True)[:2000]


def action_preview(action: Action) -> str:
    """Short stable label for events (never the full diff)."""
    name = action.action
    if name == "patch" and getattr(action, "operation", "") == "apply":
        diff = getattr(action, "diff", "")
        return f"patch apply ({len(diff)} chars)"
    return f"{name}"
