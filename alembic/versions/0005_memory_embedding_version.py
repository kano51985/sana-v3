"""Record the exact embedding model version used by memory migration."""

from alembic import op
import sqlalchemy as sa


revision = "0005_memory_embedding_version"
down_revision = "0004_memory_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_embeddings",
        sa.Column(
            "model_version",
            sa.String(length=100),
            server_default="legacy-unknown",
            nullable=False,
        ),
    )
    op.drop_constraint(
        "uq_memory_embeddings_item_model",
        "memory_embeddings",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_memory_embeddings_item_model_version",
        "memory_embeddings",
        ["memory_item_id", "model", "model_version"],
    )
    op.alter_column("memory_embeddings", "model_version", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "uq_memory_embeddings_item_model_version",
        "memory_embeddings",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_memory_embeddings_item_model",
        "memory_embeddings",
        ["memory_item_id", "model"],
    )
    op.drop_column("memory_embeddings", "model_version")
