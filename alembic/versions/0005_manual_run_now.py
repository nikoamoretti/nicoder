"""manual run now: trigger type and idempotency on planned collection runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "planned_collection_runs",
        sa.Column("trigger_type", sa.String(length=30), nullable=False, server_default="scheduled"),
    )
    op.alter_column(
        "planned_collection_runs",
        "status",
        type_=sa.String(length=30),
        existing_type=sa.String(length=20),
        existing_nullable=False,
        server_default="planned",
    )
    op.add_column(
        "planned_collection_runs",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "planned_collection_runs",
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "planned_collection_runs",
        sa.Column("requested_by", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "planned_collection_runs",
        sa.Column("cohort_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "planned_collection_runs",
        sa.Column("expected_request_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "planned_collection_runs",
        sa.Column("maximum_incremental_cost_usd", sa.Float(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_planned_run_idempotency", "planned_collection_runs", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_planned_run_idempotency", "planned_collection_runs", type_="unique")
    op.drop_column("planned_collection_runs", "maximum_incremental_cost_usd")
    op.drop_column("planned_collection_runs", "expected_request_count")
    op.drop_column("planned_collection_runs", "cohort_fingerprint")
    op.drop_column("planned_collection_runs", "requested_by")
    op.drop_column("planned_collection_runs", "requested_at")
    op.drop_column("planned_collection_runs", "idempotency_key")
    op.alter_column(
        "planned_collection_runs",
        "status",
        type_=sa.String(length=20),
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )
    op.drop_column("planned_collection_runs", "trigger_type")
