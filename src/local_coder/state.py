"""Runtime-owned canonical episode state.

Only the controller mutates this object. The model never writes state,
grants edit permissions, changes commands, or declares tests green. The
briefing is re-rendered from this state every turn; the conversation is
never appended to the prompt.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .config import Acceptance, Budgets

STATE_SCHEMA_VERSION = 1

PHASES = ("INIT", "DISCOVER", "EDIT", "VERIFY", "RECOVER", "TERMINAL")
OUTCOMES = ("running", "solved", "blocked", "setup_failed", "runtime_failed", "discarded")


class FocusRange(BaseModel):
    """One permitted source range with its content identity."""

    model_config = ConfigDict(extra="forbid")
    path: str
    start: int
    end: int
    sha256: str
    reason: str


class PatchRecord(BaseModel):
    """Identity of one runtime-applied patch plus its reverse data pointer."""

    model_config = ConfigDict(extra="forbid")
    sha: str
    summary: str
    files: list[str]
    base_tree: str
    result_tree: str
    reverse_artifact: str
    after: dict[str, str] = Field(default_factory=dict)


class CheckEvidence(BaseModel):
    """Structured, controller-owned test evidence for one invocation."""

    model_config = ConfigDict(extra="forbid")
    scope: str
    argv: list[str]
    exit_code: int | None
    status: str  # pass | fail | incomplete
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    collected: list[str] = Field(default_factory=list)
    collection_error: str | None = None
    duration_s: float = 0.0
    failure_summary: str | None = None
    relevant_frames: list[str] = Field(default_factory=list)
    log_artifact: str = ""
    report_artifact: str | None = None
    tree_hash: str = ""
    truncated: bool = False
    incomplete_reason: str | None = None


class EpisodeState(BaseModel):
    """Complete objective episode state. Single writer: the controller."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = STATE_SCHEMA_VERSION
    episode_id: str
    repo_path: str
    source_revision: str
    source_tree_hash: str
    ticket: str
    hypothesis: str | None = None
    focus: list[FocusRange] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    last_action: str | None = None
    last_patch: PatchRecord | None = None
    patch_history: list[PatchRecord] = Field(default_factory=list)
    baseline: CheckEvidence | None = None
    current: CheckEvidence | None = None
    final: CheckEvidence | None = None
    required_tests: list[str] = Field(default_factory=list)
    regression: bool = False
    phase: str = "INIT"
    generations_used: int = 0
    patch_attempts: int = 0
    model_checks: int = 0
    test_invocations: int = 0
    final_validations: int = 0
    consecutive_invalid: int = 0
    repeat_streak: int = 0
    budgets: Budgets = Field(default_factory=Budgets)
    registry: dict[str, list[str]] = Field(default_factory=dict)
    registry_hash: str = ""
    acceptance: Acceptance = Field(default_factory=Acceptance)
    provenance: dict[str, str] = Field(default_factory=dict)
    outcome: str = "running"
    stop_reason: str | None = None
    fingerprints: list[str] = Field(default_factory=list)
    prompt_tokens_est: int = 0
    completion_tokens_est: int = 0
    created_ts: str = ""
    updated_ts: str = ""
    candidate_tree_hash: str = ""

    def remaining_generations(self) -> int:
        """Model generation slots still available."""
        return max(0, self.budgets.max_generations - self.generations_used)
