"""The brands cutover (ADR-0028): mint a brand per legacy restaurant.

Per legacy row (kind='branch' AND brand_id IS NULL — the guard that makes
every statement re-runnable):
  1. Insert its brand: id = 'brd_' || the rst_ hex (deterministic, visibly
     paired for debugging), profile copied, version 0 — version 0 is what
     the boot backfill keys on to emit the event storm exactly once.
  2. Re-point menu_categories + menu_items to the brand: the whole menu
     becomes the base menu. Item ids do not change, so stock rows, carts
     and order snapshots are untouched.
  3. Copy cuisines to the brand (it becomes the cuisine authority; the
     branch keeps its denormalized copy for browse).
  4. Stamp the branch: brand_id + branch_label 'Main'.
Then swap the owner unique to brand rows only (one BRAND per owner, many
branches) and add the kind⟺parent CHECK deferred from 0006.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_LEGACY = "kind = 'branch' AND brand_id IS NULL"


def upgrade() -> None:
    bind = op.get_bind()
    # The index swap comes FIRST: the minted brand copies its owner, which
    # the old one-row-per-owner unique would refuse (found live — the sqlite
    # unit suite builds final metadata and never runs this file).
    op.drop_index("ix_restaurants_owner_user_id", table_name="restaurants")
    op.create_index("ix_restaurants_owner_user_id", "restaurants", ["owner_user_id"])
    op.create_index(
        "uq_restaurants_owner_brand",
        "restaurants",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'brand'"),
        sqlite_where=sa.text("kind = 'brand'"),
    )
    bind.execute(
        sa.text(
            f"""
            INSERT INTO restaurants
                (id, owner_user_id, name, city, lat, lon, status, hours,
                 timezone, version, kind, brand_id, branch_label,
                 created_at, updated_at)
            SELECT 'brd_' || substr(id, 5), owner_user_id, name, city,
                   lat, lon, 'open', hours, timezone, 0, 'brand', NULL, NULL,
                   created_at, updated_at
            FROM restaurants WHERE {_LEGACY}
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO restaurant_cuisines (restaurant_id, cuisine)
            SELECT 'brd_' || substr(r.id, 5), rc.cuisine
            FROM restaurant_cuisines rc
            JOIN restaurants r ON r.id = rc.restaurant_id
            WHERE r.kind = 'branch' AND r.brand_id IS NULL
            """
        )
    )
    for table in ("menu_categories", "menu_items"):
        bind.execute(
            sa.text(
                f"""
                UPDATE {table} SET restaurant_id = 'brd_' || substr(restaurant_id, 5)
                WHERE restaurant_id IN (SELECT id FROM restaurants WHERE {_LEGACY})
                """
            )
        )
    bind.execute(
        sa.text(
            f"""
            UPDATE restaurants
            SET brand_id = 'brd_' || substr(id, 5), branch_label = 'Main'
            WHERE {_LEGACY}
            """
        )
    )
    op.create_check_constraint(
        "ck_restaurants_kind_parent",
        "restaurants",
        "(kind = 'brand' AND brand_id IS NULL) OR (kind = 'branch' AND brand_id IS NOT NULL)",
    )


def downgrade() -> None:
    # Deliberately destructive-safe only for a stack that never branched:
    # re-points menus back and deletes the minted brands.
    bind = op.get_bind()
    op.drop_constraint("ck_restaurants_kind_parent", "restaurants")
    op.drop_index("uq_restaurants_owner_brand", table_name="restaurants")
    op.drop_index("ix_restaurants_owner_user_id", table_name="restaurants")
    for table in ("menu_categories", "menu_items"):
        bind.execute(
            sa.text(
                f"""
                UPDATE {table} SET restaurant_id = 'rst_' || substr(restaurant_id, 5)
                WHERE restaurant_id IN (SELECT id FROM restaurants WHERE kind = 'brand')
                """
            )
        )
    bind.execute(sa.text("UPDATE restaurants SET brand_id = NULL, branch_label = NULL"))
    bind.execute(sa.text("DELETE FROM restaurant_cuisines WHERE restaurant_id LIKE 'brd_%'"))
    bind.execute(sa.text("DELETE FROM restaurants WHERE kind = 'brand'"))
    op.create_index("ix_restaurants_owner_user_id", "restaurants", ["owner_user_id"], unique=True)
