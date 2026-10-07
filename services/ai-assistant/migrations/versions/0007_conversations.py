"""The conversation store (ADR-0042, FR-67, FR-69).

Three tables and one property worth reading the DDL for: `message_chunks`
hangs off `messages` hangs off `conversations` by `ON DELETE CASCADE`, so
NFR-32's 90-day purge is one `DELETE FROM conversations` and cannot leave
orphans. ADR-0042 listed "retention now means two deletes" as a cost of
persisting chunks; the cascade is what pays it back, and the ADR is amended
to say so.

`message_chunks` exists only to serve reconnects — it is the replay buffer
for a stream whose reader went away, not the record of what was said. The
assembled answer lives on `messages.content`.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_user", "conversations", ["user_id", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Text,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "idempotency_key", name="uq_messages_idempotency"),
    )
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "message_chunks",
        sa.Column(
            "message_id",
            sa.Text,
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("message_chunks")
    op.drop_table("messages")
    op.drop_table("conversations")
