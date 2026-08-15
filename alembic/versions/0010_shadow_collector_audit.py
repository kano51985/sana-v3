"""Add fenced Collector claims and structured gold assertion audit."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_shadow_collector_audit"
down_revision = "0009_shadow_campaign_gate"
branch_labels = None
depends_on = None

TENANT_TABLES = ("shadow_gold_assertion_results",)


def upgrade() -> None:
    op.add_column(
        "shadow_run_results",
        sa.Column("collector_lease_owner", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "shadow_run_results",
        sa.Column(
            "collector_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "shadow_run_results",
        sa.Column(
            "error_signal_flags",
            postgresql.ARRAY(sa.String(length=100)),
            server_default="{}",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_shadow_run_results_collector_lease_binding"),
        "shadow_run_results",
        "(collector_lease_owner IS NULL AND collector_lease_expires_at IS NULL) OR "
        "(scheduling_state = 'SUBMITTED' AND collector_lease_owner IS NOT NULL "
        "AND collector_lease_expires_at IS NOT NULL)",
    )
    op.create_index(
        "ix_shadow_results_collector_claim",
        "shadow_run_results",
        [
            "tenant_id",
            "campaign_id",
            "scheduling_state",
            "collector_lease_expires_at",
        ],
    )

    op.create_table(
        "shadow_gold_assertion_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assertion_id", sa.String(length=100), nullable=False),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_gold_assertions_campaign",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "result_id"],
            [
                "shadow_run_results.tenant_id",
                "shadow_run_results.campaign_id",
                "shadow_run_results.id",
            ],
            name="fk_shadow_gold_assertions_result",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shadow_gold_assertion_results"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_shadow_gold_assertions_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "result_id",
            "assertion_id",
            name="uq_shadow_gold_assertions_result_assertion",
        ),
        sa.CheckConstraint(
            "status IN ('PASS', 'FAIL', 'NOT_APPLICABLE')",
            name=op.f("ck_shadow_gold_assertion_results_status"),
        ),
    )
    op.create_index(
        "ix_shadow_gold_assertions_campaign",
        "shadow_gold_assertion_results",
        ["tenant_id", "campaign_id", "status"],
    )
    op.execute(
        'ALTER TABLE "shadow_gold_assertion_results" ENABLE ROW LEVEL SECURITY'
    )
    op.execute(
        'ALTER TABLE "shadow_gold_assertion_results" FORCE ROW LEVEL SECURITY'
    )
    op.execute(
        'CREATE POLICY tenant_isolation ON "shadow_gold_assertion_results" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_table("shadow_gold_assertion_results")
    op.drop_index("ix_shadow_results_collector_claim", table_name="shadow_run_results")
    op.drop_constraint(
        op.f("ck_shadow_run_results_collector_lease_binding"),
        "shadow_run_results",
        type_="check",
    )
    op.drop_column("shadow_run_results", "error_signal_flags")
    op.drop_column("shadow_run_results", "collector_lease_expires_at")
    op.drop_column("shadow_run_results", "collector_lease_owner")
