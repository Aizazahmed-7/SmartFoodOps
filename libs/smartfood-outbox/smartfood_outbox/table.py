"""The outbox schema and staging call — ONE author for the column contract.

Four services used to hand-copy this 9-column table guarded only by a
"same column contract as every outbox" comment, while OutboxPoller reads
the columns by name. The contract now lives beside its reader (the same
move smartfood-idempotency made with idempotency_table): a column change
is one edit here, not four synchronized ones.

Services keep thin repo wrappers that pin their aggregate_type — the lib
owns the shape, the service owns its identity.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from smartfood_otel import current_traceparent
from sqlalchemy.ext.asyncio import AsyncSession


def outbox_table(metadata: sa.MetaData) -> sa.Table:
    """The transactional-outbox table (ARCHITECTURE §invariant 1). The
    poller treats these columns as its wire contract; traceparent is the
    W3C context captured at staging so the async hop stays stitched."""
    return sa.Table(
        "outbox",
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
        sa.Column("aggregate_version", sa.Integer, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True, index=True),
        sa.Column("traceparent", sa.Text, nullable=True),
    )


async def stage_event(
    session: AsyncSession,
    table: sa.Table,
    *,
    aggregate_type: str,
    aggregate_id: str,
    version: int,
    event_type: str,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """Stage one event in the CALLER's transaction (never commits): the
    deterministic id + traceparent stamping every service used to repeat."""
    from . import event_id  # local import: __init__ imports poller, avoid cycles at module load

    await session.execute(
        table.insert().values(
            id=event_id(aggregate_type, aggregate_id, version, event_type),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=version,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
            traceparent=current_traceparent(),
        )
    )
