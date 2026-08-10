"""Application configuration.

Every knob lives behind environment variables prefixed with ``ENDPOINT_LEDGER_``
(or in a project-local ``.env`` file). Secrets such as ``OPENAI_API_KEY`` are
read directly from the environment by the OpenAI adapter and are never part of
this object or any persisted record.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_prefix="ENDPOINT_LEDGER_",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = "postgresql+psycopg://localhost:5432/endpoint_ledger"

    # Probes
    probes_dir: str = "probes/smoke"

    # OpenAI adapter
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 60.0
    openai_max_retries: int = 3

    # Dashboard
    show_raw_responses: bool = False

    # Runner
    cost_ceiling_usd: float = 5.0
    mock_latency_base_ms: int = 120
    mock_latency_jitter_ms: int = 15

    # Change detection
    # Minimum samples per window is 3: with per-probe-version comparison, one
    # run's repeats (3) is the natural window unit. Configurable.
    change_detection_min_observations: int = 3
    change_detection_min_absolute_change: float = 0.25
    change_detection_alpha: float = 0.05
    change_detection_split_after: str | None = None

    # Measurement integrity (Phase 1.1)
    serving_surface: str = "openai-chat-completions-v1"
    runner_compat_version: str = "0.1.0"
    baseline_min_calendar_days: int = 7
    baseline_min_distinct_runs: int = 10
    baseline_min_primary_observations: int = 50
    baseline_min_distinct_dates: int = 5

    # Evidence durability (Phase 1.2)
    backup_dir: str = ".backups"
    backup_max_age_hours: float = 24.0
    require_backup_before_maintenance: bool = True
    evidence_export_dir: str = "evidence_exports"
    # Run-level change detection (live claims)
    change_detection_min_baseline_runs: int = 10
    change_detection_min_current_runs: int = 3
    change_detection_min_current_dates: int = 2
    change_detection_material_effect: float = 0.2
    change_detection_require_direction_consistency: bool = True
    change_detection_permutations: int = 2000

    # Baseline operations (Phase 1.3)
    baseline_min_elapsed_hours: float = 168.0
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 30.0
    local_timezone: str = "UTC"
    credential_service_name: str = "endpoint-ledger"

    @property
    def backup_path(self) -> Path:
        path = Path(self.backup_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def evidence_export_path(self) -> Path:
        path = Path(self.evidence_export_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def probes_path(self) -> Path:
        path = Path(self.probes_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def split_after_datetime(self) -> datetime | None:
        """Parsed split boundary, or None."""
        if not self.change_detection_split_after:
            return None
        return datetime.fromisoformat(self.change_detection_split_after)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
