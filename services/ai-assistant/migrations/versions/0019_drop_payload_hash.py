"""Drop `knowledge_pending.payload_hash` — guard on the payload itself.

The column was a digest of `payload`, stored so the drain's completing
DELETE could be guarded cheaply: the drain reads a row, spends seconds
embedding with no transaction open, and must not then delete work that
arrived while it was away.

That guard is load-bearing and stays. Only the digest goes — `payload` is
already on the row, so the DELETE now compares it directly, which is the
same relationship `content_hash` had to `content` in 0018.

Verified on both dialects before removing it. Postgres stores JSONB and
RE-ORDERS keys on write, so a text comparison would have been wrong — but
JSONB equality is semantic, so a payload read back and passed into the WHERE
clause still matches. sqlite stores JSON as text and round-trips
deterministically. Both were exercised directly.

`fingerprint()` goes with it: it existed only to populate this column, and
its `sort_keys=True` was there to make the digest stable against exactly the
key re-ordering that JSONB equality now handles for us.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-08
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("knowledge_pending", "payload_hash")


def downgrade() -> None:
    raise NotImplementedError(
        "payload_hash cannot be reconstructed for rows already drained; "
        "the queue is derived state — truncate it and replay c1.catalog.changes"
    )
