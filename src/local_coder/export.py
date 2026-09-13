"""Trace export: versioned state/briefing-to-action records.

One JSONL record per generation turn. Raw negatives (rejected actions,
failed checks, protocol errors) are preserved alongside verified
transitions; reviewed corrections are always empty (no human corrections
are invented). Held-out evaluation records are excluded unless explicitly
requested. No training, teacher calls, or uploads happen here.
"""
from __future__ import annotations

import json
from pathlib import Path

from .persistence import EpisodeStore

EXPORT_VERSION = "1"

SKILL_ACCEPTED = {"applied", "reverted", "ok", "pass", "fail", "stopped", "solved"}


def _read_json(store: EpisodeStore, episode_id: str, turn: int, name: str):
    try:
        return json.loads(store.read_turn_file(episode_id, turn, name))
    except (OSError, ValueError):
        return None


def _reconciled(store: EpisodeStore, episode_id: str, turn: int):
    return _read_json(store, episode_id, turn, "reconciled.json")


def export_turn(store: EpisodeStore, episode_id: str, turn: int, split: str = "unsplit",
                include_held_out: bool = False) -> dict:
    """Build one export record. Incomplete turns are marked, never faked."""
    record: dict = {
        "export_version": EXPORT_VERSION,
        "episode_id": episode_id,
        "turn": turn,
        "split": split,
        "complete": False,
        "missing": [],
        "training_eligible": False,
        "verified_transition": False,
        "reviewed_corrections": [],
    }
    directory = store.turn_dir(episode_id, turn)
    if not directory.exists():
        record["missing"] = ["turn-bundle"]
        return record
    ok, bad = store.verify_turn(episode_id, turn)
    if not ok:
        record["missing"] = bad
        record["reconciled"] = _reconciled(store, episode_id, turn)
        return record
    state_pre = _read_json(store, episode_id, turn, "state-pre.json")
    try:
        briefing_text = store.read_turn_file(episode_id, turn, "briefing.txt")
    except OSError:
        briefing_text = None
    profile = _read_json(store, episode_id, turn, "profile.json")
    request = _read_json(store, episode_id, turn, "request.json")
    response = _read_json(store, episode_id, turn, "response.json")
    action = _read_json(store, episode_id, turn, "action.json")
    observation = _read_json(store, episode_id, turn, "observation.json")
    state_post = _read_json(store, episode_id, turn, "state-post.json")
    manifest = _read_json(store, episode_id, turn, "manifest.json")
    record.update({
        "complete": True,
        "protocol_version": (profile or {}).get("protocol_version", "unknown"),
        "constraint": (profile or {}).get("constraint", "unknown"),
        "counter": (profile or {}).get("counter", "unknown"),
        "briefing_sha": _sha(briefing_text),
        "briefing": briefing_text,
        "state_pre": state_pre,
        "request": request,
        "raw_response": (response or {}).get("raw"),
        "usage": (response or {}).get("usage"),
        "action": (action or {}).get("action"),
        "parse_ok": bool((action or {}).get("ok")),
        "parse_failure": None if (action or {}).get("ok") else action,
        "observation": observation,
        "state_post": state_post,
        "candidate_tree": ((state_post or {}).get("candidate_tree_hash")),
        "manifest": manifest,
        "reconciled": _reconciled(store, episode_id, turn),
    })
    obs_status = str((observation or {}).get("status", ""))
    skill_ran = obs_status in SKILL_ACCEPTED
    evidence_clean = not ((observation or {}).get("code") in ("stale_evidence",) or
                          (observation or {}).get("incomplete_reason"))
    record["verified_transition"] = bool(
        record["parse_ok"] and skill_ran and evidence_clean)
    record["training_eligible"] = bool(
        record["verified_transition"] and (split != "held-out" or include_held_out))
    return record


def _sha(text: str | None) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def episode_turns(store: EpisodeStore, episode_id: str) -> list[int]:
    """Turns with bundles, plus legacy slots derived from request events."""
    turns = store.list_turns(episode_id)
    if turns:
        return turns
    steps = [e["data"].get("step") for e in store.read_events(episode_id)
             if e["type"] == "model_request" and isinstance(e["data"].get("step"), int)]
    return sorted({s + 1 for s in steps})


def export_episode(store: EpisodeStore, episode_id: str, split: str = "unsplit",
                   include_held_out: bool = False) -> list[dict]:
    """Export every turn. Legacy bundles export as incomplete records."""
    records = []
    for turn in episode_turns(store, episode_id):
        record = export_turn(store, episode_id, turn, split=split,
                             include_held_out=include_held_out)
        if not (store.turn_dir(episode_id, turn).exists()):
            record["legacy"] = True
        records.append(record)
    return records


def write_jsonl(records: list[dict], dest: Path) -> Path:
    """Write versioned JSONL export."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return dest
