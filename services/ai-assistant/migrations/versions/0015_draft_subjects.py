"""Facts the content studio writes copy FROM (FR-88).

Two columns, one purpose: a draft must be written from a dish's name,
tags, category and cuisine, and both the index and the draft row were a
step short of being able to say so.

`item_chunks.name` — the index already holds category, tags and cuisines as
columns, but the NAME only as the first line of `content`. A consumer that
needs it structured should not parse a blob, and the pipeline has the name
in hand when it builds that line. Backfilled from the first line here
because the construction is known (`knowledge.item_text`) and the data is
genuinely there, just in prose.

`content_drafts.subject` — the facts FROZEN at request time. The worker
then needs no catalog call and no index read, and the row records exactly
what the model was told, which is the only way to review a draft later and
know whether it described the dish or something that has since changed.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add-backfill-constrain, in that order, because the column is NOT NULL
    # at rest: the chunk tables hold the contract that every column means
    # something for every row (`test_only_brand_id_is_nullable`), and every
    # dish does have a name. Adding it nullable and leaving it so would
    # trade a real invariant for a migration shortcut.
    op.add_column("item_chunks", sa.Column("name", sa.Text, nullable=True))
    # The first line of `content` IS the name, by construction
    # (`knowledge.item_text`). One-time and bounded, and it means the studio
    # works for the existing corpus rather than only for dishes re-indexed
    # after this deploy.
    op.execute(
        sa.text("UPDATE item_chunks SET name = split_part(content, chr(10), 1) WHERE name IS NULL")
    )
    op.alter_column("item_chunks", "name", nullable=False)
    op.add_column("content_drafts", sa.Column("subject", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("content_drafts", "subject")
    op.drop_column("item_chunks", "name")
