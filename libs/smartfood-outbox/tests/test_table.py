"""The outbox column contract — the poller reads these BY NAME, so the
factory pins them here, once, next to the reader."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from smartfood_outbox import outbox_table, stage_event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def test_column_contract_is_exactly_what_the_poller_reads():
    table = outbox_table(sa.MetaData())
    assert [c.name for c in table.columns] == [
        "id",
        "aggregate_type",
        "aggregate_id",
        "event_type",
        "payload",
        "occurred_at",
        "published_at",
        "traceparent",
    ]
    assert table.c.id.primary_key
    assert table.c.published_at.nullable  # NULL = not yet drained


async def _throwaway_outbox():
    metadata = sa.MetaData()
    table = outbox_table(metadata)
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), table


async def test_stage_event_writes_the_row_the_poller_expects():
    sessions, table = await _throwaway_outbox()
    now = datetime.now(UTC)
    async with sessions() as session:
        await stage_event(
            session,
            table,
            aggregate_type="order",
            aggregate_id="ord_1",
            event_type="OrderConfirmed",
            payload={"order_id": "ord_1"},
            now=now,
        )
        await session.commit()
    async with sessions() as session:
        row = (await session.execute(sa.select(table))).one()
    assert (row.aggregate_type, row.aggregate_id) == ("order", "ord_1")
    assert uuid.UUID(row.id).version == 4  # random identity (ADR-0035)
    assert row.published_at is None  # born undrained


async def test_identical_facts_get_different_ids():
    """The reversal, made explicit (ADR-0035): the id is minted, not derived.
    Nothing dedupes on re-deriving it — every caller is guarded by its own
    aggregate's uniqueness before it ever reaches stage_event, and consumers
    dedupe on the id READ FROM THE ROW, which never changes once written."""
    sessions, table = await _throwaway_outbox()
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    async with sessions() as session:
        for _ in range(2):  # the SAME fact, staged twice in one transaction
            await stage_event(
                session,
                table,
                aggregate_type="order",
                aggregate_id="ord_1",
                event_type="OrderConfirmed",
                payload={"order_id": "ord_1"},
                now=now,
            )
        await session.commit()
    async with sessions() as session:
        ids = [r.id for r in (await session.execute(sa.select(table))).all()]
    # Two rows, not an IntegrityError on the PK: the old derived id would
    # have collided here and aborted the caller's whole transaction.
    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(uuid.UUID(i).version == 4 for i in ids)
