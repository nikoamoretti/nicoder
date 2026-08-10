"""baseline operations: plans, scheduler, notifications, credentials

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DEFAULT = sa.text("'{}'::json")


def upgrade() -> None:
    op.create_table(
        "credential_metadata",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False, unique=True),
        sa.Column("storage_method", sa.String(length=40), nullable=False),
        sa.Column("configured", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validation_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "preflight_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("blocking_reasons", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("report", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_preflight_records_provider", "preflight_records", ["provider"])

    op.create_table(
        "collection_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), sa.ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("suite_name", sa.String(length=120), nullable=False),
        sa.Column("cohort_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("model_reported_at_creation", sa.String(length=255), nullable=True),
        sa.Column("repeats_per_probe", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=60), nullable=False, server_default="UTC"),
        sa.Column("schedule", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("qualification_policy", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("maximum_total_cost_usd", sa.Float(), nullable=True),
        sa.Column("maximum_request_count", sa.Integer(), nullable=True),
        sa.Column("maximum_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("maximum_runs", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="proposed"),
        sa.Column("qualification_run_id", sa.Integer(), nullable=True),
        sa.Column("cumulative_cost_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "planned_collection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("collection_plan_id", sa.Integer(), sa.ForeignKey("collection_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="planned"),
        sa.Column("blocking_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suite_run_id", sa.Integer(), sa.ForeignKey("suite_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("configuration_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("collection_plan_id", "scheduled_for", name="uq_planned_run_schedule"),
    )
    op.create_index("ix_planned_collection_runs_plan", "planned_collection_runs", ["collection_plan_id"])

    op.create_table(
        "automation_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="info"),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("collection_plan_id", sa.Integer(), nullable=True),
        sa.Column("suite_run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "scheduler_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("hostname", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="running"),
    )

    op.create_table(
        "baseline_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cohort_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("verification_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("cohort_fingerprint", name="uq_baseline_report_cohort"),
    )


def downgrade() -> None:
    op.drop_table("baseline_reports")
    op.drop_table("scheduler_state")
    op.drop_table("automation_notifications")
    op.drop_table("planned_collection_runs")
    op.drop_table("collection_plans")
    op.drop_table("preflight_records")
    op.drop_table("credential_metadata")
