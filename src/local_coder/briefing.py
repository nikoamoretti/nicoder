"""Briefing generation: rendered afresh from canonical state every turn.

The prompt never grows by appending conversation. Raw logs stay in
artifacts; the model sees compact structured observations and selected
source excerpts with line numbers and content identity. Model hypotheses
are labeled as proposals, never facts. Source comments and logs are
untrusted task data, not controller instructions.
"""
from __future__ import annotations

import json

from .inference.tokenizer import EstimateCounter, TokenCounter
from .protocol import Action
from .skills.check import compare_to_baseline
from .state import CheckEvidence, EpisodeState
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
    counter: TokenCounter | None = None,
    context_limit: int | None = None,
) -> str:
    """Render the full briefing for the next generation.

    Excerpts trim deterministically (lowest-ranked focus entries first, whole
    ranges only) until the prompt fits the cap; every omission is recorded.
    Token counts come from the loaded model's tokenizer when live
    (MEASURED) and from the chars//4 heuristic otherwise (ESTIMATE).
    """
    counter = counter or EstimateCounter()
    budgets = state.budgets
    reserve = (
        state.template_overhead_measured
        if state.template_overhead_measured is not None
        else budgets.template_reserve_tokens
    )
    limit = budgets.server_context_tokens if context_limit is None else context_limit
    cap = min(budgets.max_prompt_tokens, limit - reserve - budgets.max_action_tokens)
    label = "MEASURED" if counter.exact else "ESTIMATE"
    if cap < 1:
        raise ContextBudgetExceeded(
            f"no room for a prompt: context {limit} minus reserve {reserve} "
            f"minus {budgets.max_action_tokens} generation tokens leaves {cap}"
        )
    excerpts, omitted = _collect_excerpts(state, workspace)
    text = _assemble(state, excerpts, omitted, last_observation)
    while counter.count(text) > cap and excerpts:
        dropped = excerpts.pop()
        omitted.append(f"{dropped[0]} (trimmed to fit {cap} {label} prompt tokens)")
        text = _assemble(state, excerpts, omitted, last_observation)
    tokens = counter.count(text)
    if tokens > cap:
        raise ContextBudgetExceeded(
            f"minimum briefing is {tokens} {label} tokens, cap is {cap} "
            f"(context {limit}, reserve {reserve}, generation {budgets.max_action_tokens})"
        )
    if omitted_note is not None:
        omitted_note.extend(omitted)
    return text


def _assemble(
    state: EpisodeState,
    excerpts: list[tuple[str, str]],
    omitted: list[str],
    last_observation: dict | None,
) -> str:
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
    return "\n\n".join(sections) + "\n"


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
    return progress_text(state.baseline, state.current, state.regression)


def progress_text(
    baseline: CheckEvidence | None, current: CheckEvidence | None, regression: bool
) -> str:
    """Render progress from the shared comparison (public for tests).

    Fixed means explicitly passing in valid current evidence. Anything else
    — skipped, missing, unexecuted, or any incomplete run — is reported in
    its own category and never as a fix.
    """
    if baseline is None or current is None:
        return "PROGRESS\n(no comparison yet)"
    comparison = compare_to_baseline(baseline, current)
    if not comparison["comparable"]:
        reason = current.incomplete_reason or baseline.incomplete_reason or "incomplete evidence"
        lines = [
            "PROGRESS (test identities, not counts)",
            f"no comparable evidence ({reason}); no fix claimed",
        ]
        if comparison["missing"]:
            lines.append(f"missing: {comparison['missing']}")
        if comparison["skipped_now"]:
            lines.append(f"skipped now: {comparison['skipped_now']}")
        return "\n".join(lines)
    return (
        "PROGRESS (test identities, not counts)\n"
        f"baseline failing: {comparison['baseline_failing'] or []}\n"
        f"current failing: {comparison['current_failing'] or []}\n"
        f"fixed (explicitly passing now): {comparison['fixed'] or []}\n"
        f"still failing: {comparison['still_failing'] or []}\n"
        f"new: {comparison['new_failures'] or []} regression={regression}"
    )


# Observation keys in keep-first priority order; unknown keys trail
# alphabetically at the lowest priority.
_OBSERVATION_PRIORITY = [
    "status", "scope", "code", "warning", "detail", "reasons",
    "failed", "errors", "regression", "fixed_vs_baseline", "new_vs_baseline",
    "failure", "frames", "incomplete_reason", "passed", "skipped",
    "hits", "new_focus", "focus_limit", "note", "log", "patch", "files",
    "before", "after", "summary",
]

_OBSERVATION_BUDGET_CHARS = 2000


def compact_observation(observation: dict, budget_chars: int = _OBSERVATION_BUDGET_CHARS) -> str:
    """Serialize an observation without ever cutting JSON mid-field.

    Keys are kept in priority order; oversized lists are shortened by whole
    items with an explicit remainder count; dropped keys are named. The
    result always parses as JSON. Deterministic for identical input.
    """
    ordered = [k for k in _OBSERVATION_PRIORITY if k in observation]
    ordered += sorted(k for k in observation if k not in _OBSERVATION_PRIORITY)
    kept: dict = {}
    dropped: list[str] = []
    shortened: dict[str, int] = {}
    for key in ordered:
        value = observation[key]
        candidate = dict(kept)
        candidate[key] = value
        if len(json.dumps(candidate, sort_keys=True)) <= budget_chars:
            kept = candidate
            continue
        if isinstance(value, list) and value:
            short = list(value)
            while len(short) > 1 and len(json.dumps({**kept, key: short}, sort_keys=True)) > budget_chars:
                short.pop()
            remainder = len(value) - len(short)
            if len(json.dumps({**kept, key: short}, sort_keys=True)) <= budget_chars:
                kept[key] = short
                if remainder:
                    shortened[key] = remainder
                continue
        dropped.append(key)
    if shortened:
        kept["_shortened_lists"] = {k: f"+{n} more" for k, n in sorted(shortened.items())}
    if dropped:
        kept["_dropped_keys"] = dropped
    return json.dumps(kept, sort_keys=True)


def _observation_section(observation: dict | None) -> str:
    if not observation:
        return "LAST OBSERVATION\n(none)"
    return "LAST OBSERVATION\n" + compact_observation(observation)


def action_preview(action: Action) -> str:
    """Short stable label for events (never the full diff)."""
    name = action.action
    if name == "patch" and getattr(action, "operation", "") == "apply":
        diff = getattr(action, "diff", "")
        return f"patch apply ({len(diff)} chars)"
    return f"{name}"
