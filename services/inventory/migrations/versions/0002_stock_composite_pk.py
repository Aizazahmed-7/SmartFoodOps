"""stock PK → (restaurant_id, item_id) — one fridge count per branch.

Menu inheritance (ADR-0028) forces this: a BASE item keeps one item_id at
every branch of its brand, and each branch's provisioning event carries it.
Under the old item_id-only PK the second branch's row silently vanished
into ON CONFLICT DO NOTHING, so that branch could never sell the item
(STRICT stock reads a missing row as 0). Data-preserving: existing rows are
already unique on the pair (item_id alone was the PK). The old scope index
is dropped — restaurant_id is now the PK prefix.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("stock_pkey", "stock")
    op.create_primary_key("stock_pkey", "stock", ["restaurant_id", "item_id"])
    op.drop_index("ix_stock_restaurant_id", table_name="stock")


def downgrade() -> None:
    op.drop_constraint("stock_pkey", "stock")
    op.create_primary_key("stock_pkey", "stock", ["item_id"])
    op.create_index("ix_stock_restaurant_id", "stock", ["restaurant_id"])
