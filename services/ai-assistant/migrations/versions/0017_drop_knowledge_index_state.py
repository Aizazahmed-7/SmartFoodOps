"""Drop `knowledge_index_state` — the rolling reindex is gone.

The table existed to let RETRIEVAL read one vector space while the drain
WROTE another, which is what made a model migration possible without taking
search down. With the reindex removed and the embedding model and dimensions
fixed by configuration, the active version is always the configured one, so
the row could only ever repeat what `Settings` already says — and a second
source of truth that always agrees is a second source of truth that will one
day disagree.

`model_version` stays on the chunk tables. It costs nothing (it is already
the leading column of the retrieval indexes) and it is what stops two vector
spaces mixing in one result set if the configured model is ever changed
without rebuilding.

**The procedure that replaces the reindex**: change the model, truncate
`item_chunks` and `restaurant_chunks`, and reset the `assistant.knowledge.v1`
consumer group. `catalog.changes` is compacted, so replaying it rebuilds the
index from the latest state of every restaurant. Search answers nothing while
that runs — which is the trade accepted when the reindex was removed.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("knowledge_index_state")


def downgrade() -> None:
    op.create_table(
        "knowledge_index_state",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("active_model_version", sa.Text, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'current'", name="ck_knowledge_index_state_singleton"),
    )
