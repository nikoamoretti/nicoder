"""evidence durability: sealing, artifacts, backups guardrails

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DEFAULT = sa.text("'{}'::json")

from endpoint_ledger.db.sealed_evidence import ALL_SQL as SEALED_EVIDENCE_ALL_SQL
from endpoint_ledger.db.sealed_evidence import DROP_SQL as DROP_SEALED_EVIDENCE_SQL

def upgrade() -> None:
    # ---- suite_runs: evidence lifecycle
    op.add_column(
        "suite_runs",
        sa.Column("evidence_state", sa.String(length=20), nullable=False, server_default="open"),
    )
    op.add_column("suite_runs", sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("suite_runs", sa.Column("evidence_manifest_hash", sa.String(length=64), nullable=True))
    op.add_column("suite_runs", sa.Column("evidence_exported_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_suite_runs_evidence_state", "suite_runs", ["evidence_state"])

    # ---- baseline_cohorts: invalidation reason
    op.add_column("baseline_cohorts", sa.Column("invalidation_reason", sa.Text(), nullable=True))

    # ---- new tables
    op.create_table(
        "run_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suite_run_id", sa.Integer(), sa.ForeignKey("suite_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(length=30), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("verification_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_run_artifacts_suite_run_id", "run_artifacts", ["suite_run_id"])

    op.create_table(
        "run_annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suite_run_id", sa.Integer(), sa.ForeignKey("suite_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("annotation_type", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewer_label", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_run_annotations_suite_run_id", "run_annotations", ["suite_run_id"])

    op.create_table(
        "maintenance_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_name", sa.String(length=60), nullable=False),
        sa.Column("target_origin", sa.String(length=20), nullable=True),
        sa.Column("affected_entity_types", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("affected_counts", sa.JSON(), nullable=False, server_default=JSON_DEFAULT),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # ---- backfill: seal terminal live runs
    op.execute(
        """
        UPDATE suite_runs SET evidence_state = 'sealed', sealed_at = completed_at
        WHERE data_origin = 'live'
          AND status IN ('completed', 'completed_with_errors', 'failed', 'cancelled')
        """
    )

    # ---- incident backfill: invalidate stale live cohorts whose observations
    # were destroyed by the 2026-08-09 demo-history data-loss incident.
    op.execute(
        """
        UPDATE baseline_cohorts SET status = 'invalidated',
            invalidation_reason = 'dev_data_loss_incident_2026_08_09'
        WHERE data_origin = 'live'
          AND status IN ('collecting', 'qualified')
          AND NOT EXISTS (
              SELECT 1 FROM observations o
              JOIN suite_runs sr ON sr.id = o.suite_run_id
              WHERE o.probe_version_id = baseline_cohorts.probe_version_id
                AND o.endpoint_id = baseline_cohorts.endpoint_id
                AND sr.data_origin = 'live'
          )
        """
    )

    # ---- database-level protection of sealed live evidence
    op.execute(SEALED_EVIDENCE_ALL_SQL)


def downgrade() -> None:
    op.execute(DROP_SEALED_EVIDENCE_SQL)
    op.drop_table("maintenance_operations")
    op.drop_table("run_annotations")
    op.drop_table("run_artifacts")
    op.drop_column("baseline_cohorts", "invalidation_reason")
    op.drop_index("ix_suite_runs_evidence_state", table_name="suite_runs")
    op.drop_column("suite_runs", "evidence_exported_at")
    op.drop_column("suite_runs", "evidence_manifest_hash")
    op.drop_column("suite_runs", "sealed_at")
    op.drop_column("suite_runs", "evidence_state")
