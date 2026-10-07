"""`knowledge_pending` — the debounce queue (FR-57, ADR-0033).

A table rather than an in-process timer or a Redis key, for three reasons
that all reduce to the same one: this queue must survive.

- A restart mid-window must not lose a menu edit. Offsets are committed as
  soon as the row is staged, so Kafka will not redeliver it.
- It is the answer to "why is this restaurant stale?" — `due_at` and
  `first_seen_at` are queryable, and an operator can see a backlog rather
  than infer one.
- Redis would add a second store to reason about for a queue whose truth
  already belongs beside the index it feeds.

`due_at` is indexed because the drain's only query is "what is due now".

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_pending",
        sa.Column("restaurant_id", sa.Text, primary_key=True),
        # JSONB, not JSON: the drain reads whole documents today, but an
        # operator debugging a backlog wants `payload->>'name'` without a
        # parse, and JSONB is the only one of the two that indexes.
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_pending_due", "knowledge_pending", ["due_at"])


def downgrade() -> None:
    op.drop_table("knowledge_pending")
