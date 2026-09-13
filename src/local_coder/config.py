"""Trusted episode configuration: budgets, command registry, acceptance.

Loaded from the target repository's local-coder.toml (written by `init`),
or from built-in defaults. The model never sees or edits this object; its
hash is stored per episode so later inspection can prove which commands and
acceptance criteria governed the run.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILENAME = "local-coder.toml"


class Budgets(BaseModel):
    """v1 engineering limits. Stored per episode; configurable before a run."""

    model_config = ConfigDict(extra="forbid")
    max_generations: int = 8
    max_focus_files: int = 4
    max_modified_files: int = 4
    max_patch_attempts: int = 3
    max_model_checks: int = 3
    baseline_invocations: int = 1
    final_invocations: int = 1
    max_test_invocations: int = 5
    max_action_tokens: int = 2048
    server_context_tokens: int = 8192
    max_prompt_tokens: int = 6144
    briefing_target_tokens: int = 4096
    # Conservative bound for chat-template overhead until a live run measures
    # it (server usage.prompt_tokens minus content tokens). Labeled ESTIMATE.
    template_reserve_tokens: int = 256
    max_patch_bytes: int = 32 * 1024
    test_timeout_s: int = 60
    max_elapsed_s: int = 15 * 60
    max_log_bytes: int = 256 * 1024
    memory_rss_limit_mb: int | None = None


class Acceptance(BaseModel):
    """Terminal correctness contract. Pre-existing failures must be named
    before the episode; they are never inferred after a result."""

    model_config = ConfigDict(extra="forbid")
    preexisting_failures: list[str] = Field(default_factory=list)


class RepoConfig(BaseModel):
    """Trusted per-repository configuration."""

    model_config = ConfigDict(extra="forbid")
    version: int = 1
    commands: dict[str, list[str]] = Field(default_factory=lambda: default_commands())
    editable_paths: list[str] = Field(default_factory=lambda: ["src/**/*.py"])
    readonly_paths: list[str] = Field(default_factory=lambda: ["tests/**/*.py"])
    test_paths: list[str] = Field(default_factory=lambda: ["tests"])
    retrieval_allow: list[str] = Field(default_factory=lambda: ["**"])
    retrieval_deny: list[str] = Field(default_factory=list)
    execution: str = "isolated"
    budgets: Budgets = Field(default_factory=Budgets)
    acceptance: Acceptance = Field(default_factory=Acceptance)

    def registry_hash(self) -> str:
        """Hash of the command registry plus acceptance contract."""
        payload = json.dumps(
            {"commands": self.commands, "acceptance": self.acceptance.model_dump()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def default_commands(python: str = "python3") -> dict[str, list[str]]:
    """Default pytest registry for small Python repositories."""
    base = [python, "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
    return {
        "baseline": list(base),
        "focused": list(base),
        "regression": list(base),
        "final": list(base),
    }


EXAMPLE_TOML = """\
# Local Coder repository configuration (trusted, human-edited).
# The model cannot read or change this file during an episode.
version = 1
execution = "trusted-local"  # "isolated" (default) or "trusted-local" (explicit, NOT a sandbox)
editable_paths = ["src/**/*.py"]
readonly_paths = ["tests/**/*.py", "conftest.py", "local-coder.toml"]
test_paths = ["tests"]
retrieval_allow = ["**"]
retrieval_deny = []

[commands]
baseline = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
focused = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
regression = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]
final = ["python3", "-m", "pytest", "tests/", "-q", "--tb=short", "-p", "no:cacheprovider"]

[acceptance]
preexisting_failures = []

[budgets]
# All defaults are documented in config.py; uncomment to override.
# max_generations = 8
# max_focus_files = 4
# max_modified_files = 4
# max_patch_attempts = 3
# max_model_checks = 3
# max_test_invocations = 5
# test_timeout_s = 60
# max_elapsed_s = 900
"""


def load_config(repo: Path) -> RepoConfig:
    """Load trusted config; fall back to defaults when no file exists."""
    path = repo / CONFIG_FILENAME
    if not path.exists():
        return RepoConfig(commands=default_commands())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    try:
        return RepoConfig.model_validate(data)
    except Exception as exc:
        raise ValueError(f"invalid {path}: {exc}") from exc


def write_example_config(repo: Path, overwrite: bool = False) -> Path:
    """Write an editable example config. Refuses to overwrite by default."""
    path = repo / CONFIG_FILENAME
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; use --force to overwrite")
    path.write_text(EXAMPLE_TOML, encoding="utf-8")
    return path
