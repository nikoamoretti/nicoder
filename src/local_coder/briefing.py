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
from .spans import Span, build_catalogue, render_spans
from .state import CheckEvidence, EpisodeState
from .workspace import CandidateWorkspace

ACTION_SCHEMA_V1 = """\
Choose exactly ONE action as a single JSON object (no fences, no list):
{"action":"find","query":"...","symbols":["Name"]} |
{"action":"patch","operation":"apply","diff":"<unified diff>","expected_effect":"..."} |
{"action":"patch","operation":"revert_last","reason_code":"..."} |
{"action":"check","scope":"focused|regression"} |
{"action":"done","status":"solved","summary":"..."} |
{"action":"done","status":"blocked","reason_code":"...","summary":"..."}
PATCH may touch only current focus files. CHECK runs a registered scope."""

# Backwards-compatible alias: v1 is the default protocol.
ACTION_SCHEMA = ACTION_SCHEMA_V1

ACTION_SCHEMA_V3 = (
    "Choose exactly ONE action as a single JSON object (no fences, no list).\n"
    "Protocol v3: PATCH uses span selection. Pick span_id values ONLY from the\n"
    "SPANS section below and write only the new code. Never reproduce old text,\n"
    "calculate hashes, provide offsets or line numbers, or author diff headers.\n"
    '{"action":"find","query":"...","symbols":["Name"]} |\n'
    '{"action":"patch","operation":"replace_span",'
    '"edits":[{"span_id":"S1","new":"<replacement source text>"}],'
    '"expected_effect":"..."} |\n'
    '{"action":"patch","operation":"revert_last","reason_code":"..."} |\n'
    '{"action":"check","scope":"focused|regression"} |\n'
    '{"action":"done","status":"solved","summary":"..."} |\n'
    '{"action":"done","status":"blocked","reason_code":"...","summary":"..."}\n'
    "PATCH replace_span may address only the listed span IDs.\n"
    "CHECK runs a registered scope."
)

ACTION_SCHEMA_V2 = (
    "Choose exactly ONE action as a single JSON object (no fences, no list).\n"
    "Protocol v2: PATCH uses exact text replacement. Never author unified diffs,\n"
    "hashes, line numbers, hunk counts, or Git headers. Copy `old` text exactly\n"
    "from the excerpts below, including whitespace.\n"
    '{"action":"find","query":"...","symbols":["Name"]} |\n'
    '{"action":"patch","operation":"replace",'
    '"edits":[{"file_id":1,"old":"<exact existing text>",'
    '"new":"<replacement text>"}],"expected_effect":"..."} |\n'
    '{"action":"patch","operation":"revert_last","reason_code":"..."} |\n'
    '{"action":"check","scope":"focused|regression"} |\n'
    '{"action":"done","status":"solved","summary":"..."} |\n'
    '{"action":"done","status":"blocked","reason_code":"...","summary":"..."}\n'
    "PATCH replace may touch only current focus files, addressed by file_id.\n"
    "CHECK runs a registered scope."
)

# Task-independent example (a toy greeting function; never a ticket answer).
V2_EXAMPLE = (
    "Example replace (toy file only, not this ticket):\n"
    "excerpt shows:\n"
    "--- 1:src/toy.py:1-3 sha=abc123 ---\n"
    "1:def hello(name):\n"
    '2:    return "hi " + name\n'
    "action:\n"
    '{"action":"patch","operation":"replace",'
    '"edits":[{"file_id":1,"old":"    return \\"hi \\" + name",'
    '"new":"    return \\"hello, \\" + name"}],'
    '"expected_effect":"Friendlier greeting"}'
)

# Task-independent example (a toy greeting function; never a ticket answer).
V3_SPAN_EXAMPLE = (
    "Example replace_span (toy file only, not this ticket):\n"
    "spans show:\n"
    "[S1] src/toy.py bytes 17-45 sha=abc123\n"
    '    return "hi " + name\n'
    "action:\n"
    '{"action":"patch","operation":"replace_span",'
    '"edits":[{"span_id":"S1","new":"    return \\"hello, \\" + name"}],'
    '"expected_effect":"Friendlier greeting"}'
)


def constraint_notes(state: EpisodeState) -> list[str]:
    """Advertised action availability derived from actual budgets and state.

    Used to render the CONSTRAINTS section so the model is never offered an
    action the controller would have to refuse. Empty means all four actions
    are currently available.
    """
    notes = []
    b = state.budgets
    if state.patch_attempts >= b.max_patch_attempts:
        notes.append(
            f"PATCH unavailable: patch budget spent "
            f"({state.patch_attempts}/{b.max_patch_attempts}). FIND, CHECK, DONE remain."
        )
    if state.model_checks >= b.max_model_checks or state.test_invocations >= b.max_test_invocations:
        notes.append(
            "CHECK unavailable: check budget spent "
            f"(model {state.model_checks}/{b.max_model_checks}, "
            f"invocations {state.test_invocations}/{b.max_test_invocations}). "
            "FIND, PATCH, DONE remain."
        )
    if state.pending_verification is not None:
        notes.append(
            "verification pending: the runtime runs the mandatory automatic check "
            "before the next model decision."
        )
    return notes


class ContextBudgetExceeded(Exception):
    """Even the minimum valid prompt cannot fit the token budget."""


def render(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    last_observation: dict | None,
    omitted_note: list[str] | None = None,
    counter: TokenCounter | None = None,
    context_limit: int | None = None,
    protocol_version: str = "1",
) -> str:
    """Render the full briefing for the next generation (text only)."""
    text, _, omitted = render_full(
        state, workspace, last_observation,
        counter=counter, context_limit=context_limit, protocol_version=protocol_version,
    )
    if omitted_note is not None:
        omitted_note.extend(omitted)
    return text


def render_full(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    last_observation: dict | None,
    counter: TokenCounter | None = None,
    context_limit: int | None = None,
    protocol_version: str = "1",
) -> tuple[str, list[Span], list[str]]:
    """Render the briefing plus the exact span list it displays.

    Returns (text, spans, omitted). The controller persists this exact span
    list as the request's bindings, so IDs always mean what the model saw.
    Span IDs are positional and stable: trimming drops trailing spans only
    and never renumbers survivors.
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
    spans: list[Span] = []
    if protocol_version == "3":
        spans, span_omitted = _collect_spans(state, workspace, excerpts)
        omitted.extend(span_omitted)
    text = _assemble(state, excerpts, omitted, last_observation, protocol_version, spans, workspace)
    if protocol_version == "3":
        while counter.count(text) > cap and spans:
            dropped_span = spans.pop()
            omitted.append(
                f"span {dropped_span.span_id} ({dropped_span.path}) trimmed "
                f"to fit {cap} {label} prompt tokens")
            text = _assemble(state, excerpts, omitted, last_observation, protocol_version, spans,
                             workspace)
    else:
        while counter.count(text) > cap and excerpts:
            dropped_excerpt = excerpts.pop()
            omitted.append(f"{dropped_excerpt[0]} (trimmed to fit {cap} {label} prompt tokens)")
            text = _assemble(state, excerpts, omitted, last_observation, protocol_version, spans,
                             workspace)
    tokens = counter.count(text)
    if tokens > cap:
        raise ContextBudgetExceeded(
            f"minimum briefing is {tokens} {label} tokens, cap is {cap} "
            f"(context {limit}, reserve {reserve}, generation {budgets.max_action_tokens})"
        )
    return text, spans, omitted


def _collect_spans(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    excerpts: list[tuple[str, str]],
) -> tuple[list[Span], list[str]]:
    """Build the request span catalogue from surviving excerpt ranges."""
    ranges = [(foc.path, foc.start, foc.end) for foc in state.focus
              if any(path == foc.path for path, _ in excerpts)]
    if not ranges:
        return [], []
    budget_chars = state.budgets.briefing_target_tokens * 2
    spans, omitted, _bindings = build_catalogue(workspace.root, ranges, budget_chars)
    return spans, omitted


def _assemble(
    state: EpisodeState,
    excerpts: list[tuple[str, str]],
    omitted: list[str],
    last_observation: dict | None,
    protocol_version: str = "1",
    spans: list[Span] | None = None,
    workspace: CandidateWorkspace | None = None,
) -> str:
    if protocol_version == "3":
        schema = ACTION_SCHEMA_V3
    elif protocol_version == "2":
        schema = ACTION_SCHEMA_V2
    else:
        schema = ACTION_SCHEMA_V1
    sections = [
        f"TASK\n{state.ticket.strip()}",
        _budget_section(state),
        "CURRENT HYPOTHESIS (model proposal, not a fact)\n" + (state.hypothesis or "(none)"),
        _focus_section(state, excerpts, omitted, protocol_version),
    ]
    if protocol_version == "3":
        sections.append(_spans_section(spans or [], workspace))
    sections.extend([
        _last_change_section(state),
        _check_section(state),
        _progress_section(state),
        _observation_section(last_observation),
        _constraints_section(state),
        "RULES\n" + schema,
    ])
    if protocol_version == "2":
        sections.append(V2_EXAMPLE)
    if protocol_version == "3":
        sections.append(V3_SPAN_EXAMPLE)
    return "\n\n".join(section for section in sections if section) + "\n"


def _spans_section(spans: list[Span], workspace: CandidateWorkspace | None) -> str:
    lines = ["SPANS (address PATCH replace_span edits by span_id; write only the new code)"]
    if not spans:
        lines.append("(no editable spans; use FIND to add focus evidence)")
        return "\n".join(lines)
    lines.append(render_spans(workspace.root, spans) if workspace is not None else "")
    return "\n".join(lines)


def _constraints_section(state: EpisodeState) -> str:
    notes = constraint_notes(state)
    if not notes:
        return ""
    return "CONSTRAINTS\n" + "\n".join(f"- {note}" for note in notes)


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


def _focus_section(
    state: EpisodeState, excerpts: list[tuple[str, str]], omitted: list[str], protocol_version: str = "1"
) -> str:
    if not state.focus:
        return "FILES\n(no focus files yet; use FIND first)"
    lines = ["FILES"]
    by_path = dict(excerpts)
    for file_id, foc in enumerate(state.focus, start=1):
        if protocol_version == "2":
            lines.append(
                f"[id={file_id}] {foc.path}:{foc.start}-{foc.end} sha={foc.sha256[:12]} — {foc.reason}"
            )
        else:
            lines.append(f"{foc.path}:{foc.start}-{foc.end} sha={foc.sha256[:12]} — {foc.reason}")
        if foc.path in by_path:
            lines.append(by_path[foc.path])
    for note in omitted:
        lines.append(f"[omitted excerpt: {note}]")
    return "\n".join(lines)


def focus_bindings(state: EpisodeState) -> dict[int, dict[str, str]]:
    """Runtime file-ID bindings for the current focus order.

    IDs are positional (1-based) over state.focus. The controller snapshots
    this mapping into the turn bundle before dispatch and resolves replace
    edits against the snapshot, so IDs always mean the exact paths and full
    source hashes the model saw in that request's briefing.
    """
    return {file_id: {"path": foc.path, "sha256": foc.sha256} for file_id, foc in enumerate(state.focus, start=1)}


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
    if name == "patch" and getattr(action, "operation", "") == "replace":
        edits = getattr(action, "edits", [])
        return f"patch replace ({len(edits)} edits)"
    return f"{name}"
