"""Drop `assistant_facts.cache_tier` — the answer cache is gone.

The column split FR-95's "average AI response time" into generated and
cached, because a 4 ms cache hit averaged against a 2 s generation describes
neither. With the answer cache removed from the assistant every turn is a
generation, so the split has one bucket and the column only ever holds "".

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("assistant_facts", "cache_tier")


def downgrade() -> None:
    op.add_column(
        "assistant_facts",
        sa.Column("cache_tier", sa.Text, nullable=False, server_default=""),
    )
