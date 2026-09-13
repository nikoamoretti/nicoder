"""PATCH: transactional, focus-limited, path-safe, reversible edits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..paths import sha256_file
from ..state import EpisodeState, PatchRecord
from ..workspace import CandidateWorkspace, PatchValidationError


def _focus_map(state: EpisodeState) -> dict[str, str]:
    return {f.path: f.sha256 for f in state.focus}


def check_stale(state: EpisodeState, workspace: CandidateWorkspace) -> list[str]:
    """Focus files whose candidate bytes no longer match recorded hashes."""
    stale: list[str] = []
    for foc in state.focus:
        full = workspace.root / foc.path
        if not full.exists():
            stale.append(foc.path)
        elif sha256_file(full) != foc.sha256:
            stale.append(foc.path)
    return stale


def run_patch_apply(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    store_artifacts: Path,
    diff: str,
    expected_effect: str,
    editable: list[str],
    episode_id: str,
    write_artifact,
) -> tuple[dict, PatchRecord | None, dict[str, bytes] | None]:
    """Apply a unified diff atomically. Returns (observation, record, reverse)."""
    stale = check_stale(state, workspace)
    if stale:
        return (
            {"status": "rejected", "code": "stale_evidence",
             "detail": f"focus changed under us: {sorted(stale)}; re-FIND before patching"},
            None,
            None,
        )
    base_tree = workspace.tree_hash()
    try:
        result = workspace.apply_diff(
            diff, {f.path for f in state.focus}, editable, state.budgets.max_patch_bytes
        )
    except PatchValidationError as exc:
        return ({"status": "rejected", "code": exc.code, "detail": str(exc)}, None, None)
    new_files = [f for f in result.files if f not in state.modified_files]
    if len(state.modified_files) + len(new_files) > state.budgets.max_modified_files:
        workspace.revert_files(result.reverse, result.after)
        return (
            {"status": "rejected", "code": "modified_file_budget",
             "detail": f"patch would exceed {state.budgets.max_modified_files} modified files"},
            None,
            None,
        )
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16]
    reverse_ref = write_artifact(episode_id, f"patch-reverse-{digest}.json",
                                 json.dumps({k: v.hex() for k, v in result.reverse.items()}))
    record = PatchRecord(
        sha=digest,
        summary=expected_effect[:200],
        files=result.files,
        base_tree=base_tree,
        result_tree=workspace.tree_hash(),
        reverse_artifact=reverse_ref,
        after=dict(result.after),
    )
    observation = {
        "status": "applied",
        "patch": digest,
        "files": result.files,
        "before": {k: v[:12] for k, v in result.before.items()},
        "after": {k: v[:12] for k, v in result.after.items()},
    }
    return observation, record, result.reverse


def run_patch_revert(
    state: EpisodeState,
    workspace: CandidateWorkspace,
    last_reverse: dict[str, bytes] | None,
    reason_code: str,
) -> dict:
    """Restore runtime-recorded pre-patch bytes when the after-state matches."""
    if state.last_patch is None or last_reverse is None:
        return {"status": "rejected", "code": "nothing_to_revert",
                "detail": "no runtime-recorded patch to revert"}
    try:
        for rel in state.last_patch.files:
            if not (workspace.root / rel).exists():
                return {"status": "rejected", "code": "unsafe_revert",
                        "detail": f"{rel} no longer exists; refusing revert"}
        expected_after = dict(state.last_patch.after)
        if not expected_after:
            return {"status": "rejected", "code": "unsafe_revert",
                    "detail": "no recorded after-state; refusing blind revert"}
        workspace.revert_files(last_reverse, expected_after)
    except PatchValidationError as exc:
        return {"status": "rejected", "code": exc.code, "detail": str(exc)}
    return {"status": "reverted", "patch": state.last_patch.sha,
            "files": state.last_patch.files, "reason_code": reason_code}
