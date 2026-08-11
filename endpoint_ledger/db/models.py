"""Core domain models.

Design notes / deliberate deviations from the Phase 1 spec:

- ``ProbeVersion.scorers`` stores the ordered scorer names from the probe
  definition. The spec lists it among probe-definition fields but not as a
  column; we persist it so observations carry the exact scoring contract used.
- ``ProbeVersion.prompt_messages``, ``tools``, ``expected_behavior``,
  ``request_parameters`` and ``scorers`` are stored as JSON and treated as
  immutable once observations exist (enforced by a DB trigger, see migration).
- ``ChangeEvent`` is keyed at runtime by
  (endpoint_id, metric_name, probe_category, window bounds); the service
  upserts rather than duplicating events across repeated detections.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from endpoint_ledger.db.base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProviderEndpoint(Base, TimestampMixin):
    __tablename__ = "provider_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_requested: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    request_defaults: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    input_cost_per_million_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_cost_per_million_tokens: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    observations: Mapped[list[Observation]] = relationship(
        back_populates="endpoint", passive_deletes=True
    )


class Probe(Base, TimestampMixin):
    __tablename__ = "probes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stable_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    versions: Mapped[list[ProbeVersion]] = relationship(
        back_populates="probe",
        cascade="all, delete-orphan",
        order_by="ProbeVersion.version_number",
    )

    @property
    def latest_version(self) -> ProbeVersion | None:
        if not self.versions:
            return None
        return self.versions[-1]


class ProbeVersion(Base, TimestampMixin):
    __tablename__ = "probe_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    probe_id: Mapped[int] = mapped_column(
        ForeignKey("probes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    tools: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    expected_behavior: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    scorers: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # Measurement integrity (Phase 1.1)
    baseline_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ineligibility_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_metric: Mapped[str | None] = mapped_column(String(80), nullable=True)

    probe: Mapped[Probe] = relationship(back_populates="versions")
    observations: Mapped[list[Observation]] = relationship(
        back_populates="probe_version", passive_deletes=True
    )

    __table_args__ = (
        # One version chain per probe with monotonically increasing numbers.
        UniqueConstraint("probe_id", "version_number", name="uq_probe_version_number"),
    )


class SuiteRun(Base, TimestampMixin):
    __tablename__ = "suite_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_filter: Mapped[str | None] = mapped_column(String(50), nullable=True)
    endpoint_filter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_expected_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    runner_version: Mapped[str] = mapped_column(String(30), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Measurement integrity (Phase 1.1): live / mock / synthetic / demo / test.
    data_origin: Mapped[str] = mapped_column(String(20), nullable=False, default="live", index=True)
    # Evidence durability (Phase 1.2): open -> sealed -> exported / export_failed.
    evidence_state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    observations: Mapped[list[Observation]] = relationship(
        back_populates="suite_run", cascade="all, delete-orphan", passive_deletes=True
    )


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_run_id: Mapped[int] = mapped_column(
        ForeignKey("suite_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    probe_version_id: Mapped[int] = mapped_column(
        ForeignKey("probe_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repeat_index: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_reported: Mapped[str | None] = mapped_column(String(255), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Measurement integrity (Phase 1.1)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pricing_input_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_output_price_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_effective_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cost_calculation_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cost_is_estimated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    suite_run: Mapped[SuiteRun] = relationship(back_populates="observations")
    endpoint: Mapped[ProviderEndpoint] = relationship(back_populates="observations")
    probe_version: Mapped[ProbeVersion] = relationship(back_populates="observations")
    scores: Mapped[list[Score]] = relationship(
        back_populates="observation", cascade="all, delete-orphan", passive_deletes=True
    )


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("observations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scorer_name: Mapped[str] = mapped_column(String(80), nullable=False)
    scorer_version: Mapped[str] = mapped_column(String(30), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    numeric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Measurement integrity (Phase 1.1): primary / diagnostic / informational.
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)

    observation: Mapped[Observation] = relationship(back_populates="scores")
    adjudications: Mapped[list[ScoreAdjudication]] = relationship(
        back_populates="score", cascade="all, delete-orphan", passive_deletes=True
    )


class ScoreAdjudication(Base, TimestampMixin):
    """Lightweight failure classification attached to a Score.

    The original Score row is never overwritten; adjudications are separate
    evidence records used by run audits and baseline qualification.
    """

    __tablename__ = "score_adjudications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score_id: Mapped[int] = mapped_column(
        ForeignKey("scores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_label: Mapped[str | None] = mapped_column(String(80), nullable=True)

    score: Mapped[Score] = relationship(back_populates="adjudications")


class ChangeEvent(Base, TimestampMixin):
    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    probe_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    baseline_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_value: Mapped[float] = mapped_column(Float, nullable=False)
    current_value: Mapped[float] = mapped_column(Float, nullable=False)
    absolute_change: Mapped[float] = mapped_column(Float, nullable=False)
    relative_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_level: Mapped[float] = mapped_column(Float, nullable=False)
    affected_observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="provisional")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Measurement integrity (Phase 1.1): demo events carry their origin.
    data_origin: Mapped[str] = mapped_column(String(20), nullable=False, default="live", index=True)

    endpoint: Mapped[ProviderEndpoint] = relationship()


class BaselineCohort(Base, TimestampMixin):
    """A comparison-safe set of observations sharing every cohort dimension."""

    __tablename__ = "baseline_cohorts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_requested: Mapped[str] = mapped_column(String(255), nullable=False)
    model_reported: Mapped[str | None] = mapped_column(String(255), nullable=True)
    serving_surface: Mapped[str] = mapped_column(String(80), nullable=False)
    endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=True
    )
    probe_stable_key: Mapped[str] = mapped_column(String(120), nullable=False)
    probe_version_id: Mapped[int] = mapped_column(
        ForeignKey("probe_versions.id", ondelete="CASCADE"), nullable=False
    )
    primary_metric: Mapped[str] = mapped_column(String(80), nullable=False)
    primary_scorer_name: Mapped[str] = mapped_column(String(80), nullable=False)
    primary_scorer_version: Mapped[str] = mapped_column(String(30), nullable=False)
    runner_version: Mapped[str] = mapped_column(String(30), nullable=False)
    data_origin: Mapped[str] = mapped_column(String(20), nullable=False, default="live")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="collecting")
    # Phase 1.2: why a cohort was invalidated (e.g. the 2026-08-09 data-loss incident).
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    probe_version: Mapped[ProbeVersion] = relationship()
    endpoint: Mapped[ProviderEndpoint | None] = relationship()


class ModelResolutionEvent(Base, TimestampMixin):
    """A provider-reported model change for the same requested model alias."""

    __tablename__ = "model_resolution_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requested_model: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    old_model_reported: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_model_reported: Mapped[str] = mapped_column(String(255), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, default="model_alias_routing")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint(
            "requested_model",
            "new_model_reported",
            "event_type",
            name="uq_model_resolution_event",
        ),
    )


class CredentialMetadata(Base, TimestampMixin):
    """Non-secret metadata for a configured provider credential.

    The secret itself is stored only in the OS credential store (macOS
    Keychain via keyring); this row stores no part of it. Fingerprints are
    SHA-256 digests of the secret, and identity fields are the normalized
    (redacted) result of an identity-endpoint validation.
    """

    __tablename__ = "credential_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    storage_method: Mapped[str] = mapped_column(String(40), nullable=False, default="keychain")
    configured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    keychain_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_user_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    identity_email_redacted: Mapped[str | None] = mapped_column(String(120), nullable=True)
    identity_orgs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)


class PreflightRecord(Base):
    """A persisted preflight result; becomes stale when configuration changes.

    Credential binding: the preflight records the exact credential fingerprint
    and source it validated against; live execution refuses to start when the
    current credential differs (``binding_valid`` is flipped by the credential
    service whenever the credential changes while the preflight is still on
    record).
    """

    __tablename__ = "preflight_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blocking_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    credential_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_binding: Mapped[str | None] = mapped_column(String(20), nullable=True)
    binding_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    binding_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CollectionPlan(Base, TimestampMixin):
    """A user-approved automated baseline collection plan."""

    __tablename__ = "collection_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    suite_name: Mapped[str] = mapped_column(String(120), nullable=False)
    cohort_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    model_reported_at_creation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repeats_per_probe: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default="UTC")
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    qualification_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    maximum_total_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_request_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maximum_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maximum_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="proposed")
    qualification_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cumulative_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    planned_runs: Mapped[list[PlannedCollectionRun]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PlannedCollectionRun(Base):
    """One live collection run inside a plan (scheduled, manual, or smoke)."""

    __tablename__ = "planned_collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_plan_id: Mapped[int] = mapped_column(
        ForeignKey("collection_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="scheduled"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cohort_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_request_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maximum_incremental_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="planned")
    blocking_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suite_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("suite_runs.id", ondelete="SET NULL"), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    configuration_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    plan: Mapped[CollectionPlan] = relationship(back_populates="planned_runs")

    __table_args__ = (
        UniqueConstraint("collection_plan_id", "scheduled_for", name="uq_planned_run_schedule"),
        UniqueConstraint("idempotency_key", name="uq_planned_run_idempotency"),
    )


class AutomationNotification(Base):
    """In-product automation notification."""

    __tablename__ = "automation_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    collection_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    suite_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SchedulerState(Base):
    """Single-row scheduler heartbeat for the dashboard."""

    __tablename__ = "scheduler_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    hostname: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running")


class BaselineReport(Base):
    """A versioned, sealed baseline qualification report."""

    __tablename__ = "baseline_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohort_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("cohort_fingerprint", name="uq_baseline_report_cohort"),
    )


class RunArtifact(Base):
    """A portable evidence artifact (e.g. a run export bundle)."""

    __tablename__ = "run_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_run_id: Mapped[int] = mapped_column(
        ForeignKey("suite_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(30), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RunAnnotation(Base):
    """Separate annotation attached to a run (never mutates evidence)."""

    __tablename__ = "run_annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_run_id: Mapped[int] = mapped_column(
        ForeignKey("suite_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    annotation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewer_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MaintenanceOperation(Base):
    """Sanitized audit record of destructive maintenance operations."""

    __tablename__ = "maintenance_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_name: Mapped[str] = mapped_column(String(60), nullable=False)
    target_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    affected_entity_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    affected_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
