"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-09

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from endpoint_ledger.db.triggers import DROP_TRIGGERS_SQL, TRIGGERS_SQL

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DEFAULT = sa.text("'{}'::json")


def upgrade() -> None:
    op.create_table(
        "provider_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("model_requested", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("request_defaults", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("input_cost_per_million_tokens", sa.Float(), nullable=True),
        sa.Column("output_cost_per_million_tokens", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_provider_endpoints_provider", "provider_endpoints", ["provider"])

    op.create_table(
        "probes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stable_key", sa.String(length=120), nullable=False, unique=True),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_probes_category", "probes", ["category"])

    op.create_table(
        "probe_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("probe_id", sa.Integer(), sa.ForeignKey("probes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_messages", sa.JSON(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=True),
        sa.Column("expected_behavior", sa.JSON(), nullable=False),
        sa.Column("request_parameters", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("scorers", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("probe_id", "version_number", name="uq_probe_version_number"),
    )
    op.create_index("ix_probe_versions_probe_id", "probe_versions", ["probe_id"])

    op.create_table(
        "suite_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("suite_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_filter", sa.String(length=50), nullable=True),
        sa.Column("endpoint_filter", sa.String(length=255), nullable=True),
        sa.Column("total_expected_observations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completed_observations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_observations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("actual_cost_usd", sa.Float(), nullable=True),
        sa.Column("runner_version", sa.String(length=30), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_suite_runs_suite_name", "suite_runs", ["suite_name"])

    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("suite_run_id", sa.Integer(), sa.ForeignKey("suite_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), sa.ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("probe_version_id", sa.Integer(), sa.ForeignKey("probe_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repeat_index", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("model_reported", sa.String(length=255), nullable=True),
        sa.Column("finish_reason", sa.String(length=50), nullable=True),
        sa.Column("raw_request", sa.JSON(), nullable=False),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("raw_request_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_response_hash", sa.String(length=64), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("normalized_tool_calls", sa.JSON(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("error_type", sa.String(length=50), nullable=True),
        sa.Column("error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_observations_suite_run_id", "observations", ["suite_run_id"])
    op.create_index("ix_observations_endpoint_id", "observations", ["endpoint_id"])
    op.create_index("ix_observations_probe_version_id", "observations", ["probe_version_id"])

    op.create_table(
        "scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("observation_id", sa.Integer(), sa.ForeignKey("observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scorer_name", sa.String(length=80), nullable=False),
        sa.Column("scorer_version", sa.String(length=30), nullable=False),
        sa.Column("metric_name", sa.String(length=80), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("details", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_scores_observation_id", "scores", ["observation_id"])
    op.create_index("ix_scores_metric_name", "scores", ["metric_name"])

    op.create_table(
        "change_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.Integer(), sa.ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_name", sa.String(length=80), nullable=False),
        sa.Column("probe_category", sa.String(length=50), nullable=True),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("baseline_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_value", sa.Float(), nullable=False),
        sa.Column("current_value", sa.Float(), nullable=False),
        sa.Column("absolute_change", sa.Float(), nullable=False),
        sa.Column("relative_change", sa.Float(), nullable=True),
        sa.Column("p_value", sa.Float(), nullable=True),
        sa.Column("confidence_level", sa.Float(), nullable=False),
        sa.Column("affected_observation_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="provisional"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_change_events_endpoint_id", "change_events", ["endpoint_id"])
    op.create_index("ix_change_events_metric_name", "change_events", ["metric_name"])

    op.execute(TRIGGERS_SQL)


def downgrade() -> None:
    op.execute(DROP_TRIGGERS_SQL)
    op.drop_table("change_events")
    op.drop_table("scores")
    op.drop_table("observations")
    op.drop_table("suite_runs")
    op.drop_table("probe_versions")
    op.drop_table("probes")
    op.drop_table("provider_endpoints")
