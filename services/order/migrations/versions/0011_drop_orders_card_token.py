"""Drop `orders.card_token`.

Written at placement and never read back — the only reference to the stored
value anywhere was a test asserting it had been written. The instrument
reaches Payment through the Temporal workflow input (`PriceResult.card_token`),
which is also what a retried activity replays, so no path ever consulted
this column. Payment keeps its own copy in payment_db for its own lifecycle,
and capture/void key on the PSP ref rather than the token.

That left a payment instrument persisted indefinitely in a service that does
not touch the money path (ADR-0010: only Payment imports the PSP adapter),
in a database with no retention, reachable through its backups, replicas and
CDC stream. Retention without a reader.

Also resolves a contradiction: the column was NOT NULL while
`ck_orders_payment_method` admits 'COD', and a cash order has no card token.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("orders", "card_token")


def downgrade() -> None:
    # Restorable as a column, NOT as data: the tokens are gone, and inventing
    # a placeholder that looks like an instrument would be worse than an
    # obviously empty one. Nullable on the way back for that reason — the
    # original NOT NULL cannot be honoured for historical rows.
    op.add_column("orders", sa.Column("card_token", sa.Text, nullable=True))
