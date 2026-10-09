"""Drop the stream replay buffer.

`message_chunks` stored one row per SSE frame so a reconnecting reader could
be replayed what it missed (FR-69, ADR-0042). Resume is removed: a reader
that disconnects has lost the stream, and reloading shows the assembled
`messages.content` row instead.

What goes with it in code: `stream_relay` and `Snapshot` in
smartfood-realtime, the re-ticket endpoint, `Last-Event-ID` / `?after=`, and
the per-frame sequence number — which existed only so a reconnect could drop
what it already held.

What replaces it: the turn waits for a subscriber before its first frame
(`wait_for_reader`). The bus is pub/sub, so a frame published into an empty
channel is dropped — and a safety refusal or an empty retrieval is answered
with no model at all, within a millisecond of the turn starting. Without the
wait, the fastest answers would be the ones that never arrive.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-09
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("message_chunks")


def downgrade() -> None:
    raise NotImplementedError(
        "chunks are transient stream state, not a record — recreate the table "
        "from db.py if resume is ever reinstated"
    )
