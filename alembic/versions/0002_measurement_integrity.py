"""measurement integrity: roles, origins, taxonomy, cohorts

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DEFAULT = sa.text("'{}'::json")


def upgrade() -> None:
    # ---- probe_versions: measurement contract
    op.add_column(
        "probe_versions",
        sa.Column("baseline_eligible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("probe_versions", sa.Column("ineligibility_reason", sa.Text(), nullable=True))
    op.add_column("probe_versions", sa.Column("primary_metric", sa.String(length=80), nullable=True))

    # ---- scores: role
    op.add_column("scores", sa.Column("role", sa.String(length=20), nullable=True))

    # ---- suite_runs: data origin
    op.add_column(
        "suite_runs",
        sa.Column("data_origin", sa.String(length=20), nullable=False, server_default="live"),
    )
    op.create_index("ix_suite_runs_data_origin", "suite_runs", ["data_origin"])

    # ---- observations: error taxonomy + versioned pricing evidence
    op.add_column("observations", sa.Column("error_code", sa.String(length=60), nullable=True))
    op.add_column("observations", sa.Column("error_category", sa.String(length=40), nullable=True))
    op.add_column("observations", sa.Column("pricing_input_price_per_million", sa.Float(), nullable=True))
    op.add_column("observations", sa.Column("pricing_output_price_per_million", sa.Float(), nullable=True))
    op.add_column(
        "observations", sa.Column("pricing_effective_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("observations", sa.Column("cost_calculation_version", sa.String(length=30), nullable=True))
    op.add_column("observations", sa.Column("cost_is_estimated", sa.Boolean(), nullable=True))

    # ---- change_events: data origin (demo separation)
    op.add_column(
        "change_events",
        sa.Column("data_origin", sa.String(length=20), nullable=False, server_default="live"),
    )
    op.create_index("ix_change_events_data_origin", "change_events", ["data_origin"])

    # ---- new entities
    op.create_table(
        "score_adjudications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "score_id", sa.Integer(), sa.ForeignKey("scores.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("classification", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_label", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_score_adjudications_score_id", "score_adjudications", ["score_id"])

    op.create_table(
        "baseline_cohorts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_requested", sa.String(length=255), nullable=False),
        sa.Column("model_reported", sa.String(length=255), nullable=True),
        sa.Column("serving_surface", sa.String(length=80), nullable=False),
        sa.Column(
            "endpoint_id", sa.Integer(),
            sa.ForeignKey("provider_endpoints.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("probe_stable_key", sa.String(length=120), nullable=False),
        sa.Column(
            "probe_version_id", sa.Integer(),
            sa.ForeignKey("probe_versions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("primary_metric", sa.String(length=80), nullable=False),
        sa.Column("primary_scorer_name", sa.String(length=80), nullable=False),
        sa.Column("primary_scorer_version", sa.String(length=30), nullable=False),
        sa.Column("runner_version", sa.String(length=30), nullable=False),
        sa.Column("data_origin", sa.String(length=20), nullable=False, server_default="live"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="collecting"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "model_resolution_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requested_model", sa.String(length=255), nullable=False),
        sa.Column("old_model_reported", sa.String(length=255), nullable=True),
        sa.Column("new_model_reported", sa.String(length=255), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False, server_default="model_alias_routing"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "requested_model", "new_model_reported", "event_type", name="uq_model_resolution_event"
        ),
    )
    op.create_index(
        "ix_model_resolution_events_requested_model", "model_resolution_events", ["requested_model"]
    )

    # =====================================================================
    # Backfills (preserve all existing records; never rewrite evidence)
    # =====================================================================

    # suite_runs.data_origin: demo suites, mock runs, live runs.
    op.execute(
        """
        UPDATE suite_runs SET data_origin = CASE
            WHEN suite_name ILIKE 'demo-%' THEN 'demo'
            WHEN provider_filter = 'mock' THEN 'mock'
            WHEN provider_filter = 'openai' THEN 'live'
            ELSE 'test'
        END
        """
    )

    # change_events.data_origin: events derived from mock endpoints are demo.
    op.execute(
        """
        UPDATE change_events SET data_origin = 'demo'
        WHERE endpoint_id IN (SELECT id FROM provider_endpoints WHERE provider = 'mock')
        """
    )

    # probe_versions.primary_metric: canonical mapping per category/behavior.
    op.execute(
        """
        UPDATE probe_versions pv SET primary_metric = CASE
            WHEN p.category = 'structured_output' THEN 'structured_output_task_success'
            WHEN p.category = 'tool_calling' THEN 'tool_task_success'
            WHEN pv.expected_behavior->>'item_count' IS NOT NULL THEN 'instruction_item_count_success'
            ELSE 'confirmation_behavior_success'
        END
        FROM probes p
        WHERE p.id = pv.probe_id
        """
    )

    # scores.role: primary matches the probe version's primary metric;
    # latency is informational; everything else is diagnostic.
    op.execute(
        """
        UPDATE scores s SET role = CASE
            WHEN s.metric_name = 'latency_measurement' THEN 'informational'
            WHEN s.metric_name = pv.primary_metric THEN 'primary'
            ELSE 'diagnostic'
        END
        FROM observations o, probe_versions pv
        WHERE o.id = s.observation_id AND pv.id = o.probe_version_id
        """
    )

    # Probe version baseline eligibility: the two probes whose measurement
    # contract cannot be satisfied by the single-invocation harness.
    op.execute(
        """
        UPDATE probe_versions SET baseline_eligible = false,
            ineligibility_reason = (
                'probe expects a two-step tool sequence (lookup then cancel) but the '
                'harness performs a single provider invocation per observation and never '
                'sends tool results back to the model; recorded results measure only '
                'single-turn behavior'
            )
        WHERE probe_id IN (SELECT id FROM probes WHERE stable_key = 'tool_calling_lookup_then_cancel')
        """
    )
    op.execute(
        """
        UPDATE probe_versions SET baseline_eligible = false,
            ineligibility_reason = (
                'expected action identifier is not specified in the probe prompt; the '
                'exact-value check measures prompt completeness rather than model behavior'
            )
        WHERE probe_id IN (SELECT id FROM probes WHERE stable_key = 'instruction_adherence_confirmation_before_destructive')
        """
    )

    # observations: classify the run-5 insufficient-quota incident.
    op.execute(
        """
        UPDATE observations SET error_code = 'insufficient_quota',
            error_category = 'account_configuration'
        WHERE error_type = 'RateLimitError'
          AND error_message_sanitized ILIKE '%insufficient_quota%'
        """
    )

    # observations: versioned pricing evidence. Historic runs used the OpenAI
    # endpoint pricing of $0.15 / $0.60 per million tokens.
    op.execute(
        """
        UPDATE observations SET
            pricing_input_price_per_million = 0.15,
            pricing_output_price_per_million = 0.60,
            pricing_effective_at = completed_at,
            cost_calculation_version = '1.0.0',
            cost_is_estimated = false
        WHERE estimated_cost_usd IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE observations SET
            pricing_input_price_per_million = 0.15,
            pricing_output_price_per_million = 0.60,
            pricing_effective_at = completed_at,
            cost_calculation_version = '1.0.0',
            cost_is_estimated = true
        WHERE estimated_cost_usd IS NULL
          AND suite_run_id IN (SELECT id FROM suite_runs WHERE provider_filter = 'openai')
        """
    )


def downgrade() -> None:
    op.drop_table("model_resolution_events")
    op.drop_table("baseline_cohorts")
    op.drop_table("score_adjudications")
    op.drop_index("ix_change_events_data_origin", table_name="change_events")
    op.drop_column("change_events", "data_origin")
    op.drop_column("observations", "cost_is_estimated")
    op.drop_column("observations", "cost_calculation_version")
    op.drop_column("observations", "pricing_effective_at")
    op.drop_column("observations", "pricing_output_price_per_million")
    op.drop_column("observations", "pricing_input_price_per_million")
    op.drop_column("observations", "error_category")
    op.drop_column("observations", "error_code")
    op.drop_index("ix_suite_runs_data_origin", table_name="suite_runs")
    op.drop_column("suite_runs", "data_origin")
    op.drop_column("scores", "role")
    op.drop_column("probe_versions", "primary_metric")
    op.drop_column("probe_versions", "ineligibility_reason")
    op.drop_column("probe_versions", "baseline_eligible")
