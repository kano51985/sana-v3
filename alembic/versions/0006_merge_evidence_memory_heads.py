"""Merge evidence-offset and memory-embedding migration branches."""


revision = "0006_merge_evidence_memory_heads"
down_revision = (
    "0005_evidence_offsets",
    "0005_memory_embedding_version",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
