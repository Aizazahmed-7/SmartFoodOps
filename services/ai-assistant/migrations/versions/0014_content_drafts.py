"""The content studio's one table (FR-88..FR-93).

Generated copy lands HERE and never in `menu_items`. That is what makes
"never a half-written menu" structural rather than a rule somebody follows:
there is no code path from a draft job to the catalog, only from a human's
approve action.

`status` is the job's whole state — `parked` is the dead-letter queue, as a
row rather than a broker artifact, because UC-25 asks for failures that are
"parked and visible, replayable". A message in a broker DLQ is visible to
an operator with a console; a row is visible to the restaurant whose copy
never arrived, and replaying it is an UPDATE.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

KINDS = ("menu_item", "promotion", "engagement")
STATUSES = ("queued", "drafted", "parked", "published", "rejected")


def upgrade() -> None:
    op.create_table(
        "content_drafts",
        sa.Column("draft_id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("brand_id", sa.Text, nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("target_id", sa.Text, nullable=True),
        sa.Column("request", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("published_content", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(f"kind IN {KINDS!r}", name="ck_content_drafts_kind"),
        sa.CheckConstraint(f"status IN {STATUSES!r}", name="ck_content_drafts_status"),
    )
    op.create_index(
        "ix_content_drafts_restaurant",
        "content_drafts",
        ["restaurant_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_content_drafts_restaurant", table_name="content_drafts")
    op.drop_table("content_drafts")
