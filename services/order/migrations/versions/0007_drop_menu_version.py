"""Drop `orders.menu_version`.

Placement consents to a total now (ADR-0036), so nothing pins a menu
version; and versions are being removed everywhere (ADR-0037 onward), so the
number this column stored will not exist to refer back to. The order's
`pricing_snapshot` is the durable record of what was actually charged.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("orders", "menu_version")


def downgrade() -> None:
    # 0 for every historical row: the menu version an order was priced
    # against is genuinely unrecoverable once dropped, and inventing a
    # plausible-looking number would be worse than an obvious placeholder.
    op.add_column(
        "orders", sa.Column("menu_version", sa.Integer, nullable=False, server_default="0")
    )
