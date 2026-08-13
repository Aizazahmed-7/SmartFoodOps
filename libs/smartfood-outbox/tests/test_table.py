"""The outbox column contract — the poller reads these BY NAME, so the
factory pins them here, once, next to the reader."""

from datetime import UTC, datetime

import sqlalchemy as sa
from smartfood_outbox import event_id, outbox_table, stage_event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def test_column_contract_is_exactly_what_the_poller_reads():
    table = outbox_table(sa.MetaData())
    assert [c.name for c in table.columns] == [
        "id",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "event_type",
        "payload",
        "occurred_at",
        "published_at",
        "traceparent",
    ]
    assert table.c.id.primary_key
    assert table.c.published_at.nullable  # NULL = not yet drained


async def test_stage_event_writes_the_deterministic_identity():
    metadata = sa.MetaData()
    table = outbox_table(metadata)
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sessions() as session:
        await stage_event(
            session,
            table,
            aggregate_type="order",
            aggregate_id="ord_1",
            version=3,
            event_type="OrderConfirmed",
            payload={"order_id": "ord_1"},
            now=now,
        )
        await session.commit()
    async with sessions() as session:
        row = (await session.execute(sa.select(table))).one()
    assert row.id == event_id("order", "ord_1", 3, "OrderConfirmed")
    assert (row.aggregate_type, row.aggregate_id, row.aggregate_version) == ("order", "ord_1", 3)
    assert row.published_at is None  # born undrained
