"""The idempotency table factory — each service owns its OWN copy inside its
own database (order_db, payment_db), because the row must be able to join
the service's business transaction. A shared central table would reintroduce
the cross-service-transaction problem idempotency exists to solve."""

import sqlalchemy as sa


def idempotency_table(metadata: sa.MetaData, name: str = "idempotency_keys") -> sa.Table:
    return sa.Table(
        name,
        metadata,
        # scope = who the key belongs to (user sub for HTTP, "money" for
        # payment ops): two users reusing the same UUID must never collide.
        sa.Column("scope", sa.Text, primary_key=True),
        sa.Column("idem_key", sa.Text, primary_key=True),
        sa.Column("body_hash", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="IN_PROGRESS"),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", sa.JSON, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('IN_PROGRESS','COMPLETE')", name=f"ck_{name}_status"),
    )
