"""Brands, dormant half (ADR-0028): the columns land, nothing uses them yet.

restaurants gains kind (server_default 'branch' IS the legacy backfill),
brand_id (self-FK), branch_label (+ per-brand unique — the branch-create
idempotency key), and the presence-only branch_item_overrides table (a row
means "this branch is not serving this base item"; restore = DELETE).

Deliberately NOT here, deferred to 0007 (the cutover): the brand⟺no-parent
CHECK (0006-era legacy rows are branch/NULL, which that CHECK forbids) and
the owner-unique swap to a brand-only partial index (until brands exist,
onboarding still mints plain rows and must keep its race backstop).

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("kind", sa.Text(), nullable=False, server_default="branch"),
    )
    op.create_check_constraint("ck_restaurants_kind", "restaurants", "kind IN ('brand', 'branch')")
    op.add_column(
        "restaurants",
        sa.Column("brand_id", sa.Text(), sa.ForeignKey("restaurants.id"), nullable=True),
    )
    op.create_index("ix_restaurants_brand_id", "restaurants", ["brand_id"])
    op.add_column("restaurants", sa.Column("branch_label", sa.Text(), nullable=True))
    op.create_index(
        "uq_restaurants_branch_label", "restaurants", ["brand_id", "branch_label"], unique=True
    )
    op.create_table(
        "branch_item_overrides",
        sa.Column("branch_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
        sa.Column("item_id", sa.Text, sa.ForeignKey("menu_items.id"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("branch_item_overrides")
    op.drop_index("uq_restaurants_branch_label", table_name="restaurants")
    op.drop_column("restaurants", "branch_label")
    op.drop_index("ix_restaurants_brand_id", table_name="restaurants")
    op.drop_column("restaurants", "brand_id")
    op.drop_constraint("ck_restaurants_kind", "restaurants")
    op.drop_column("restaurants", "kind")
