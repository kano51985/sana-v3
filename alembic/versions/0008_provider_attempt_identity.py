"""Make provider attempt identity include the provider."""

from alembic import op


revision = "0008_provider_attempt_identity"
down_revision = "0007_deepseek_quality_pipeline"
branch_labels = None
depends_on = None

TENANT_TABLES = ()


def upgrade() -> None:
    op.drop_constraint(
        "uq_provider_attempts_query_number",
        "provider_attempts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_provider_attempts_query_provider_number",
        "provider_attempts",
        ["query_spec_id", "provider", "attempt_no"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_provider_attempts_query_provider_number",
        "provider_attempts",
        type_="unique",
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY query_spec_id
                       ORDER BY attempt_no, provider, started_at, id
                   ) AS new_attempt_no
            FROM provider_attempts
        )
        UPDATE provider_attempts AS target
        SET attempt_no = ranked.new_attempt_no
        FROM ranked
        WHERE target.id = ranked.id
        """
    )
    op.create_unique_constraint(
        "uq_provider_attempts_query_number",
        "provider_attempts",
        ["query_spec_id", "attempt_no"],
    )
