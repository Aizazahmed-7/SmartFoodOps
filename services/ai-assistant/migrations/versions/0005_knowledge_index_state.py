"""`knowledge_index_state` — which vector space retrieval reads (FR-61).

One row, and the reason it exists at all is that "the version the drain
writes" and "the version retrieval reads" are different questions during a
rolling reindex. Without the split, bumping `embedding_model` would point
every query at a generation that is still being built — an index that
answers with whatever fraction of the corpus has been migrated so far, and
recovers silently enough that nobody notices it was ever wrong.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_index_state",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("active_model_version", sa.Text, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'current'", name="ck_knowledge_index_state_singleton"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_index_state")
