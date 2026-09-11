"""Split the branch-only columns out of `restaurants` into `branch_metadata`.

ADDITIVE: the columns stay on `restaurants` so reads can migrate one at a
time; a later revision drops them. Writes go to both from this point on.

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
    op.create_table(
        "branch_metadata",
        sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
        sa.Column("brand_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("branch_label", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("lat", sa.Float, nullable=True),
        sa.Column("lon", sa.Float, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.CheckConstraint("status IN ('open', 'paused')", name="ck_branch_metadata_status"),
        sa.Column("hours", sa.JSON, nullable=True),
        sa.Column("timezone", sa.Text, nullable=False, server_default="America/Chicago"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_branch_metadata_brand_id", "branch_metadata", ["brand_id"])
    op.create_index("ix_branch_metadata_city", "branch_metadata", ["city"])
    # Moved wholesale from restaurants: the branch-create idempotency key.
    op.create_index(
        "uq_branch_metadata_label", "branch_metadata", ["brand_id", "branch_label"], unique=True
    )

    # One row per BRANCH. Brands get none — that absence is what makes the
    # columns unrepresentable on a brand rather than conventionally absent.
    op.get_bind().execute(
        sa.text("""
        INSERT INTO branch_metadata (
            restaurant_id, brand_id, branch_label, city, lat, lon,
            status, hours, timezone, updated_at
        )
        SELECT id, brand_id, branch_label, city, lat, lon,
               status, hours, timezone, updated_at
          FROM restaurants
         WHERE kind = 'branch'
    """)
    )


def downgrade() -> None:
    # The source columns were never dropped by this revision, so `restaurants`
    # is still authoritative on the way back and nothing needs copying.
    op.drop_index("uq_branch_metadata_label", table_name="branch_metadata")
    op.drop_index("ix_branch_metadata_city", table_name="branch_metadata")
    op.drop_index("ix_branch_metadata_brand_id", table_name="branch_metadata")
    op.drop_table("branch_metadata")
