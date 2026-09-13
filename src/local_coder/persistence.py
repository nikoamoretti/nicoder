"""Episode persistence: atomic state, append-only events, file artifacts.

Layout per episode under <state_dir>/<episode_id>/:
  state.json, briefing.txt, events.jsonl, handoff.md,
  artifacts/*, candidate/ (disposable source snapshot),
  turns/NNNN/ (immutable per-generation evidence bundles).

Each turn bundle holds state-pre.json, briefing.txt, profile.json,
request.json, response.json, action.json, observation.json,
state-post.json, status.json, and manifest.json (sha256 of the rest).
Files already written are never modified; reconciliation appends new
records (e.g. reconciled.json) instead of overwriting.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from .state import EpisodeState

ARTIFACTS_DIR = "artifacts"
CANDIDATE_DIR = "candidate"
TURNS_DIR = "turns"

# Evidence required for an exact-input training record. Turns missing any
# of these are exported as incomplete and excluded from training exports.
TURN_REQUIRED_FILES = (
    "state-pre.json",
    "briefing.txt",
    "profile.json",
    "request.json",
    "response.json",
    "action.json",
    "observation.json",
    "state-post.json",
)


def utc_now() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_episode_id() -> str:
    """Fresh episode identifier."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"ep-{stamp}-{secrets.token_hex(2)}"


class EpisodeStore:
    """Filesystem-backed episode store with atomic state writes."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def episode_dir(self, episode_id: str) -> Path:
        """Directory owning one episode's records."""
        return self.state_dir / episode_id

    def candidate_dir(self, episode_id: str) -> Path:
        """Disposable candidate workspace for one episode."""
        return self.episode_dir(episode_id) / CANDIDATE_DIR

    def artifacts_dir(self, episode_id: str) -> Path:
        """Artifact directory for one episode."""
        return self.episode_dir(episode_id) / ARTIFACTS_DIR

    def create(self, episode_id: str) -> Path:
        """Create episode directories. Returns the episode directory."""
        root = self.episode_dir(episode_id)
        (root / ARTIFACTS_DIR).mkdir(parents=True, exist_ok=True)
        (root / CANDIDATE_DIR).mkdir(parents=True, exist_ok=True)
        (root / "events.jsonl").write_text("", encoding="utf-8")
        return root

    def state_path(self, episode_id: str) -> Path:
        """Path to canonical state.json."""
        return self.episode_dir(episode_id) / "state.json"

    def save_state(self, state: EpisodeState) -> None:
        """Atomically persist canonical state."""
        state.updated_ts = utc_now()
        path = self.state_path(state.episode_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def load_state(self, episode_id: str) -> EpisodeState:
        """Load state and verify the event sequence is intact."""
        path = self.state_path(episode_id)
        if not path.exists():
            raise FileNotFoundError(f"no such episode: {episode_id}")
        state = EpisodeState.model_validate_json(path.read_text(encoding="utf-8"))
        self.verify_events(episode_id)
        return state

    def events_path(self, episode_id: str) -> Path:
        """Path to the append-only event log."""
        return self.episode_dir(episode_id) / "events.jsonl"

    def append_event(self, episode_id: str, event_type: str, data: dict | None = None) -> int:
        """Append one sequenced event. Returns the sequence number."""
        path = self.events_path(episode_id)
        seq = 0
        if path.exists() and path.stat().st_size:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        seq = json.loads(line).get("seq", seq) + 1
        record = {"seq": seq, "ts": utc_now(), "type": event_type, "data": data or {}}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return seq

    def read_events(self, episode_id: str) -> list[dict]:
        """Read all events in sequence order."""
        path = self.events_path(episode_id)
        events: list[dict] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        events.append(json.loads(line))
        return events

    def verify_events(self, episode_id: str) -> int:
        """Verify append-only sequence continuity. Returns event count."""
        events = self.read_events(episode_id)
        for index, event in enumerate(events):
            if event.get("seq") != index:
                raise ValueError(f"event log corrupted at index {index} in {episode_id}")
        return len(events)

    def write_artifact_text(self, episode_id: str, name: str, text: str) -> str:
        """Store a text artifact. Returns the artifact-relative reference."""
        dest = self.artifacts_dir(episode_id) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
        return f"{ARTIFACTS_DIR}/{name}"

    def write_artifact_bytes(self, episode_id: str, name: str, data: bytes) -> str:
        """Store a binary artifact. Returns the artifact-relative reference."""
        dest = self.artifacts_dir(episode_id) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        return f"{ARTIFACTS_DIR}/{name}"

    def read_artifact_text(self, episode_id: str, ref: str) -> str:
        """Read a text artifact by its reference."""
        return (self.episode_dir(episode_id) / ref).read_text(encoding="utf-8")

    def write_briefing(self, episode_id: str, text: str) -> None:
        """Write the latest generated briefing (rendered afresh each turn)."""
        path = self.episode_dir(episode_id) / "briefing.txt"
        tmp = path.with_suffix(".txt.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def write_handoff(self, episode_id: str, text: str) -> None:
        """Write the human-readable handoff document."""
        path = self.episode_dir(episode_id) / "handoff.md"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # -- per-generation turn bundles --------------------------------------

    def turn_dir(self, episode_id: str, turn: int) -> Path:
        """Immutable evidence directory for one generation slot."""
        return self.episode_dir(episode_id) / TURNS_DIR / f"{turn:04d}"

    def list_turns(self, episode_id: str) -> list[int]:
        """Sorted turn numbers with a bundle directory present."""
        root = self.episode_dir(episode_id) / TURNS_DIR
        if not root.exists():
            return []
        turns = []
        for entry in root.iterdir():
            if entry.is_dir() and len(entry.name) == 4 and entry.name.isdigit():
                turns.append(int(entry.name))
        return sorted(turns)

    def write_turn_file(self, episode_id: str, turn: int, name: str, text: str) -> str:
        """Atomically write one turn-bundle file. Never overwrites silently."""
        dest = self.turn_dir(episode_id, turn) / name
        if dest.exists():
            raise FileExistsError(f"turn record exists and is immutable: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)
        return f"{TURNS_DIR}/{turn:04d}/{name}"

    def read_turn_file(self, episode_id: str, turn: int, name: str) -> str:
        """Read one turn-bundle file."""
        return (self.turn_dir(episode_id, turn) / name).read_text(encoding="utf-8")

    def write_turn_manifest(self, episode_id: str, turn: int) -> dict:
        """Hash every bundle file into manifest.json. Returns the manifest."""
        directory = self.turn_dir(episode_id, turn)
        entries = {}
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.name != "manifest.json":
                digest = hashlib.sha256(entry.read_bytes()).hexdigest()
                entries[entry.name] = digest
        manifest = {"turn": turn, "files": entries}
        path = directory / "manifest.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return manifest

    def verify_turn(self, episode_id: str, turn: int) -> tuple[bool, list[str]]:
        """Recompute bundle hashes. Returns (ok, missing_or_bad_files)."""
        directory = self.turn_dir(episode_id, turn)
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            return False, ["manifest.json"]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            return False, ["manifest.json"]
        bad = []
        for name, want in manifest.get("files", {}).items():
            path = directory / name
            if not path.exists():
                bad.append(name)
            elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
                bad.append(name)
        for name in TURN_REQUIRED_FILES:
            if name not in manifest.get("files", {}):
                bad.append(name)
        return (not bad), sorted(set(bad))
