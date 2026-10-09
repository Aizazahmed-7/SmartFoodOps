"""Drop the answer cache, its fence, and the idempotency key.

Three removals, all chosen to shrink what has to be explained rather than
what the assistant can do:

- `answer_cache` and `knowledge_epochs` — FR-74's two tiers and the per-city
  counter that fenced them. Every turn now calls the model. Answers are
  unchanged; a repeated question costs a generation again.
- `messages.idempotency_key` — a retried POST now starts a SECOND generation
  rather than attaching to the one already running. That is a real cost and
  a real second answer, accepted deliberately.

The explanation-polish layer went with them in code (no schema).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-09
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("answer_cache")
    op.drop_table("knowledge_epochs")
    op.drop_constraint("uq_messages_idempotency", "messages", type_="unique")
    op.drop_column("messages", "idempotency_key")


def downgrade() -> None:
    raise NotImplementedError(
        "the cache is derived state and the keys are gone — recreate the tables "
        "from db.py and let them refill"
    )
