"""Transactional accept and discard. Single implementation for CLI and app.

The original tree changes only after every gate passes. Discard removes the
candidate workspace and leaves evidence in place unless explicitly asked.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .controller import git_is_dirty
from .persistence import EpisodeStore
from .state import EpisodeState


def os_replace(tmp: Path, dst: Path) -> None:
    """Atomic rename helper (single import point for tests)."""
    os.replace(tmp, dst)


def rollback_accept(repo: Path, before: dict[str, bytes | None], applied: list[str]) -> int:
    """Restore originals for files replaced before an accept failure."""
    restored = 0
    for rel in applied:
        dst = repo / rel
        original = before.get(rel)
        try:
            if original is None:
                dst.unlink(missing_ok=True)
            else:
                tmp = dst.with_suffix(dst.suffix + ".acceptrollback")
                tmp.write_bytes(original)
                os_replace(tmp, dst)
            restored += 1
        except OSError:
            pass
    return restored


def accept_gate(store: EpisodeStore, state: EpisodeState, repo: Path) -> tuple[bool, str]:
    """All acceptance preconditions; any failure refuses without changes."""
    from .paths import candidate_manifest, original_manifest, sha256_file, tree_hash

    if state.outcome != "solved" or state.last_patch is None:
        return False, (f"episode is {state.outcome}; only solved episodes can be accepted "
                       "(blocked episodes can still be inspected and diffed)")
    if state.final is None or state.final.status != "pass":
        return False, "final validation evidence is missing or not passing"
    if not repo.is_dir():
        return False, f"repo not found: {repo}"
    if (repo / ".git").exists() and git_is_dirty(repo):
        return False, "refusing accept onto a dirty checkout"
    current_orig = tree_hash(original_manifest(repo))
    if current_orig != state.source_tree_hash:
        return False, "original tree drifted since the episode; refusing accept"
    candidate = store.candidate_dir(state.episode_id)
    if not candidate.is_dir():
        return False, "candidate workspace is missing (was it discarded?)"
    if tree_hash(candidate_manifest(candidate)) != state.candidate_tree_hash:
        return False, "candidate changed after final validation; re-verify first"
    for rel, want in (state.last_patch.after or {}).items():
        full = candidate / rel
        if not full.exists():
            return False, f"candidate file missing: {rel}"
        if sha256_file(full) != want:
            return False, f"candidate file changed: {rel}"
    return True, ""


def accept_plan(state: EpisodeState, repo: Path) -> dict[str, dict[str, object]]:
    """Describe the files that would be written on accept."""
    files = list(state.last_patch.files) if state.last_patch else []
    return {rel: {
        "candidate_sha256": state.last_patch.after.get(rel, "") if state.last_patch else "",
        "original_exists": (repo / rel).exists(),
    } for rel in files}


def accept_episode(store: EpisodeStore, episode_id: str, repo: Path, *, dry_run: bool = False) -> dict:
    """Apply a solved candidate onto the original tree, or refuse."""
    state = store.load_state(episode_id)
    repo = repo.expanduser().resolve()
    ok, detail = accept_gate(store, state, repo)
    if not ok:
        return {"ok": False, "detail": detail, "applied": []}
    files = list(state.last_patch.files) if state.last_patch else []
    plan = accept_plan(state, repo)
    if dry_run:
        return {"ok": True, "dry_run": True, "files": files, "plan": plan, "applied": []}
    candidate = store.candidate_dir(episode_id)
    store.append_event(episode_id, "accept_intent", {"files": files, "repo": str(repo)})
    before: dict[str, bytes | None] = {}
    applied: list[str] = []
    try:
        for rel in files:
            dst = repo / rel
            before[rel] = dst.read_bytes() if dst.exists() else None
            src = candidate / rel
            if not src.exists():
                raise OSError(f"candidate file missing: {src}")
            tmp = dst.with_suffix(dst.suffix + ".accepttmp")
            try:
                tmp.write_bytes(src.read_bytes())
                os_replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)
            applied.append(rel)
    except OSError as exc:
        restored = rollback_accept(repo, before, applied)
        store.append_event(episode_id, "accept_failed",
                           {"detail": str(exc), "rolled_back": restored})
        return {"ok": False, "detail": f"acceptance failed ({exc}); restored {restored} file(s)",
                "applied": [], "rolled_back": restored}
    store.append_event(episode_id, "accepted", {"files": applied, "repo": str(repo)})
    return {"ok": True, "applied": applied, "repo": str(repo)}


def discard_episode(store: EpisodeStore, episode_id: str, *, with_evidence: bool = False) -> dict:
    """Remove the candidate workspace; preserve evidence unless asked."""
    state = store.load_state(episode_id)
    repo = Path(state.repo_path)
    candidate = store.candidate_dir(episode_id)
    removed_candidate = candidate.exists()
    if removed_candidate:
        shutil.rmtree(candidate)
    state.outcome = "discarded"
    state.stop_reason = "human discard; candidate removed"
    state.phase = "TERMINAL"
    store.save_state(state)
    store.append_event(episode_id, "discarded", {"candidate_removed": removed_candidate})
    evidence_deleted = False
    if with_evidence:
        shutil.rmtree(store.episode_dir(episode_id))
        evidence_deleted = True
    return {
        "ok": True,
        "candidate_removed": removed_candidate,
        "repo": str(repo),
        "evidence_deleted": evidence_deleted,
    }
