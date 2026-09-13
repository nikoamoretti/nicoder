"""DONE: a model's 'solved' is only a request for validation."""
from __future__ import annotations

from ..paths import matches_any
from ..state import EpisodeState
from ..workspace import CandidateWorkspace


def validate_solved(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    original_manifest: dict[str, str],
    editable: list[str],
) -> tuple[bool, list[str]]:
    """Check every acceptance condition for the exact final source state."""
    reasons: list[str] = []
    if state.baseline is None or state.baseline.status == "incomplete":
        reasons.append("no completed baseline validation")
    if state.last_patch is None:
        reasons.append("no runtime-applied patch to accept")
    final_tree = workspace.tree_hash()
    evidence = state.final or state.current
    if evidence is None:
        reasons.append("no CHECK evidence on the final tree; run CHECK after the last patch")
    elif evidence.tree_hash != final_tree:
        reasons.append("stale evidence: source changed after the last CHECK; re-run CHECK")
    elif evidence.status == "incomplete":
        reasons.append(f"last CHECK is incomplete: {evidence.incomplete_reason}")
    elif evidence.status != "pass":
        bad = evidence.failed + evidence.errors
        reasons.append(f"required checks failing: {bad[:5]}")
    current_manifest = workspace.manifest()
    changed = sorted(
        rel for rel in set(original_manifest) | set(current_manifest)
        if original_manifest.get(rel) != current_manifest.get(rel)
    )
    prohibited = [rel for rel in changed if not matches_any(rel, editable)]
    if prohibited:
        reasons.append(f"prohibited edits outside editable paths: {prohibited[:5]}")
    if len(changed) > state.budgets.max_modified_files:
        reasons.append(f"changed {len(changed)} files, budget is {state.budgets.max_modified_files}")
    if evidence is not None and evidence.status == "pass":
        missing = [t for t in state.required_tests if t not in evidence.collected]
        if missing:
            reasons.append(f"required tests missing from final collection: {missing[:5]}")
    return (not reasons), reasons
