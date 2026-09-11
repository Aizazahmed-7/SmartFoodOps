"""Drop the branch-only columns from `restaurants` (the cleanup half of 0008).

`branch_metadata` has been authoritative for reads since C1b; this deletes
the source copies. After this revision a brand has no city, no address, no
hours, no timezone and no status — those are unrepresentable on a brand
rather than meaningless NULLs, which was the point of the split.

`kind` deliberately STAYS: uq_restaurants_owner_brand is a partial unique
index predicated on it, and a partial-index predicate cannot contain a
subquery, so it cannot be derived from "has a metadata row".

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_MOVED = ("city", "lat", "lon", "status", "hours", "timezone", "brand_id", "branch_label")


def upgrade() -> None:
    bind = op.get_bind()

    # The invariant no constraint enforces ("every branch has a metadata
    # row") is spent HERE — once, loudly, at the point where being wrong
    # becomes irreversible. A branch missing its row is about to lose its
    # city, hours and parent for good, so refuse to run rather than drop.
    orphans = (
        bind.execute(
            sa.text("""
        SELECT r.id FROM restaurants r
         WHERE r.kind = 'branch'
           AND NOT EXISTS (SELECT 1 FROM branch_metadata bm WHERE bm.restaurant_id = r.id)
         ORDER BY r.id LIMIT 20
    """)
        )
        .scalars()
        .all()
    )
    if orphans:
        raise RuntimeError(
            "refusing to drop the source columns: these branches have no "
            f"branch_metadata row and would lose their data: {list(orphans)}. "
            "Backfill them from `restaurants` first (see 0008's INSERT)."
        )
    # The mirror could have drifted only via a write that bypassed the repo.
    # Cheap to check while both copies still exist; impossible afterwards.
    drifted = (
        bind.execute(
            sa.text("""
        SELECT r.id FROM restaurants r
         JOIN branch_metadata bm ON bm.restaurant_id = r.id
         WHERE (r.city, r.status, r.timezone) IS DISTINCT FROM (bm.city, bm.status, bm.timezone)
            OR r.lat IS DISTINCT FROM bm.lat
            OR r.lon IS DISTINCT FROM bm.lon
            OR r.brand_id IS DISTINCT FROM bm.brand_id
            OR r.branch_label IS DISTINCT FROM bm.branch_label
         ORDER BY r.id LIMIT 20
    """)
        )
        .scalars()
        .all()
    )
    if drifted:
        raise RuntimeError(
            "refusing to drop the source columns: the two copies disagree for "
            f"{list(drifted)}. Reconcile before dropping — after this the "
            "branch_metadata value is all that survives."
        )

    # Explicit and BEFORE the columns: Postgres would cascade these away with
    # the column, but then the downgrade would have no symmetric statement.
    op.drop_constraint("ck_restaurants_status", "restaurants", type_="check")
    op.drop_constraint("ck_restaurants_kind_parent", "restaurants", type_="check")
    op.drop_constraint("restaurants_brand_id_fkey", "restaurants", type_="foreignkey")
    op.drop_index("uq_restaurants_branch_label", table_name="restaurants")
    op.drop_index("ix_restaurants_brand_id", table_name="restaurants")
    op.drop_index("ix_restaurants_city", table_name="restaurants")
    for column in _MOVED:
        op.drop_column("restaurants", column)


def downgrade() -> None:
    bind = op.get_bind()
    for column, type_ in (
        ("city", sa.Text),
        ("lat", sa.Float),
        ("lon", sa.Float),
        ("status", sa.Text),
        ("hours", sa.JSON),
        ("timezone", sa.Text),
        ("brand_id", sa.Text),
        ("branch_label", sa.Text),
    ):
        op.add_column("restaurants", sa.Column(column, type_, nullable=True))

    # Branches: restored exactly, branch_metadata is the truth.
    bind.execute(
        sa.text("""
        UPDATE restaurants r SET
            city = bm.city, lat = bm.lat, lon = bm.lon, status = bm.status,
            hours = bm.hours, timezone = bm.timezone,
            brand_id = bm.brand_id, branch_label = bm.branch_label
          FROM branch_metadata bm
         WHERE bm.restaurant_id = r.id
    """)
    )
    # Brands: FABRICATED. city/status/timezone are NOT NULL pre-0009 and a
    # brand has no such values, so this reinstates exactly the lie 0007 told
    # — one of its branches' city, chosen deterministically. Nothing reads a
    # brand's city; this exists only to satisfy the old NOT NULL.
    bind.execute(
        sa.text("""
        UPDATE restaurants r SET
            city = COALESCE((
                SELECT bm.city FROM branch_metadata bm
                 WHERE bm.brand_id = r.id ORDER BY bm.restaurant_id LIMIT 1
            ), 'unknown'),
            timezone = COALESCE((
                SELECT bm.timezone FROM branch_metadata bm
                 WHERE bm.brand_id = r.id ORDER BY bm.restaurant_id LIMIT 1
            ), 'America/Chicago'),
            status = 'open'
         WHERE r.kind = 'brand'
    """)
    )
    for column, default in (
        ("city", None),
        ("status", "'open'"),
        ("timezone", "'America/Chicago'"),
    ):
        op.alter_column("restaurants", column, nullable=False)
        if default is not None:
            op.execute(f"ALTER TABLE restaurants ALTER COLUMN {column} SET DEFAULT {default}")

    op.create_index("ix_restaurants_city", "restaurants", ["city"])
    op.create_index("ix_restaurants_brand_id", "restaurants", ["brand_id"])
    op.create_index(
        "uq_restaurants_branch_label", "restaurants", ["brand_id", "branch_label"], unique=True
    )
    op.create_foreign_key(
        "restaurants_brand_id_fkey", "restaurants", "restaurants", ["brand_id"], ["id"]
    )
    op.create_check_constraint(
        "ck_restaurants_status", "restaurants", "status IN ('open', 'paused')"
    )
    op.create_check_constraint(
        "ck_restaurants_kind_parent",
        "restaurants",
        "(kind = 'brand' AND brand_id IS NULL) OR (kind = 'branch' AND brand_id IS NOT NULL)",
    )
