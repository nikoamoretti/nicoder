"""credential controls: active source, fingerprints, identity, preflight binding

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "credential_metadata",
        sa.Column("active_source", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("keychain_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("environment_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("validation_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("validation_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("identity_user_id", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("identity_email_redacted", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "credential_metadata",
        sa.Column("identity_orgs", sa.JSON(), nullable=True),
    )
    op.add_column(
        "preflight_records",
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "preflight_records",
        sa.Column("source_binding", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "preflight_records",
        sa.Column("binding_valid", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "preflight_records",
        sa.Column("binding_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    for table, columns in (
        ("preflight_records", ("binding_reason", "binding_valid", "source_binding", "credential_fingerprint")),
        ("credential_metadata", ("identity_orgs", "identity_email_redacted", "identity_user_id",
                                 "validation_message", "validation_status", "environment_fingerprint",
                                 "keychain_fingerprint", "active_source")),
    ):
        for column in columns:
            op.drop_column(table, column)