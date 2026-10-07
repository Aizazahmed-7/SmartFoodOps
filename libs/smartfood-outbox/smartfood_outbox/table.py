"""The outbox schema and staging call — ONE author for the column contract.

Four services used to hand-copy this 9-column table guarded only by a
"same column contract as every outbox" comment, while OutboxPoller reads
the columns by name. The contract now lives beside its reader (the same
move smartfood-idempotency made with idempotency_table): a column change
is one edit here, not four synchronized ones.

Services keep thin repo wrappers that pin their aggregate_type — the lib
owns the shape, the service owns its identity.
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from smartfood_otel import current_traceparent
from sqlalchemy.ext.asyncio import AsyncSession


def outbox_table(metadata: sa.MetaData) -> sa.Table:
    """The transactional-outbox table (ARCHITECTURE §invariant 1). The
    poller treats these columns as its wire contract; traceparent is the
    W3C context captured at staging so the async hop stays stitched.

    No `aggregate_version` since ADR-0038: it was written by every producer
    and compared by no consumer — ordering per aggregate comes from the
    Kafka topic key, and event identity stopped depending on it at
    ADR-0035."""
    return sa.Table(
        "outbox",
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
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
    event_type: str,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """Stage one event in the CALLER's transaction (never commits): the id
    + traceparent stamping every service used to repeat.

    The id is random (uuid4, ADR-0035). It does NOT dedupe: what makes a
    replayed emit harmless is that every caller writes its aggregate row
    FIRST in the same transaction, so a second attempt loses to that row's
    own PK (or to a guarded transition) and never reaches this insert. What
    consumers dedupe on is this STORED value — a re-delivered or re-polled
    row carries the same id it was minted with, which is all their dedupe
    stores ever needed."""
    await session.execute(
        table.insert().values(
            id=str(uuid.uuid4()),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
            traceparent=current_traceparent(),
        )
    )
