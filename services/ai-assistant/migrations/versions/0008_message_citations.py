"""What an answer cited, kept with the answer (FR-70, ADR-0042 §5).

Grounding strips every marker from the prose before a reader sees it, so
the ids an answer cited cannot be recovered from `content`. Without them on
the row, a reader who reconnects AFTER a turn finished replays the text and
gets no cards under it — the live reader saw them on the terminal frame,
and the reconnecting one silently did not.

`[]` as a server default rather than NULL: "cited nothing" and "we did not
record what it cited" are the same thing for every row written before this
migration, and an empty list says it without a third state to handle.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("item_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("messages", "item_ids")
