"""The placement sweeper: only stale PLACED orders get sagas, per-order
failures never shadow the backlog, and the loop lives/dies with the app."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from order.adapters.repo import OrderRepo
from order.config import Settings
from order.db import metadata
from order.main import create_app
from order.sweeper import Sweeper
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class RecordingSaga:
    def __init__(self, fail_for: set[str] | None = None):
        self.started: list[str] = []
        self.fail_for = fail_for or set()

    async def start(self, order_id: str) -> None:
        if order_id in self.fail_for:
            raise RuntimeError("temporal unreachable")
        self.started.append(order_id)


async def _harness():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _insert_order(sessions, order_id: str, *, status: str, age_seconds: float) -> None:
    placed_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with sessions() as session:
        repo = OrderRepo(session)
        await repo.insert_order(
            order_id=order_id,
            user_id="usr_1",
            restaurant_id="rst_1",
            restaurant_name="Biryani House",
            card_token="tok_ok",
            menu_version=1,
            pricing_snapshot={"total_cents": 1000, "currency": "USD"},
            address_snapshot={"line1": "12 Main St"},
            lines=[
                {
                    "menu_item_id": "itm_1",
                    "name": "Biryani",
                    "unit_price_cents": 1000,
                    "qty": 1,
                    "options": [],
                    "line_total_cents": 1000,
                }
            ],
            now=placed_at,
        )
        if status != "PLACED":
            from order.db import orders

            await session.execute(
                orders.update().where(orders.c.order_id == order_id).values(status=status)
            )
        await session.commit()


async def test_sweeps_only_stale_placed_orders():
    sessions = await _harness()
    await _insert_order(sessions, "ord_stale", status="PLACED", age_seconds=120)
    await _insert_order(sessions, "ord_fresh", status="PLACED", age_seconds=5)  # racing its
    # own placement's start call — min_age keeps hands off
    await _insert_order(sessions, "ord_moved", status="CONFIRMED", age_seconds=120)  # saga alive
    saga = RecordingSaga()
    swept = await Sweeper(sessions, saga, min_age_seconds=60).sweep_once()
    assert swept == 1
    assert saga.started == ["ord_stale"]


async def test_oldest_debts_are_paid_first():
    sessions = await _harness()
    await _insert_order(sessions, "ord_newer", status="PLACED", age_seconds=100)
    await _insert_order(sessions, "ord_older", status="PLACED", age_seconds=300)
    saga = RecordingSaga()
    await Sweeper(sessions, saga, min_age_seconds=60).sweep_once()
    assert saga.started == ["ord_older", "ord_newer"]


async def test_one_failed_start_never_shadows_the_rest():
    """Temporal hiccup on one order: log, skip, keep sweeping — the row
    stays PLACED so the next pass retries it."""
    sessions = await _harness()
    await _insert_order(sessions, "ord_bad", status="PLACED", age_seconds=300)
    await _insert_order(sessions, "ord_good", status="PLACED", age_seconds=200)
    saga = RecordingSaga(fail_for={"ord_bad"})
    swept = await Sweeper(sessions, saga, min_age_seconds=60).sweep_once()
    assert swept == 1
    assert saga.started == ["ord_good"]
    # Next pass, Temporal recovered: the skipped order is still owed.
    saga.fail_for = set()
    assert await Sweeper(sessions, saga, min_age_seconds=60).sweep_once() == 2


async def test_empty_backlog_sweeps_nothing():
    sessions = await _harness()
    saga = RecordingSaga()
    assert await Sweeper(sessions, saga).sweep_once() == 0
    assert saga.started == []


async def test_run_survives_a_failing_pass_and_cancels_cleanly():
    """The reaper/poller contract: a bad pass (DB down here) logs and
    retries; cancellation is the shutdown signal."""

    class ExplodingSessions:
        calls = 0

        def __call__(self):
            type(self).calls += 1
            raise RuntimeError("db down")

    sessions = ExplodingSessions()
    sweeper = Sweeper(sessions, RecordingSaga(), interval_seconds=0.0)  # type: ignore[arg-type]
    task = asyncio.create_task(sweeper.run())
    async with asyncio.timeout(2.0):
        # Polling stub state set by a background task — nothing to await.
        while ExplodingSessions.calls < 2:  # noqa: ASYNC110
            await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ExplodingSessions.calls >= 2  # crashed pass did not kill the loop


def test_lifespan_runs_and_cancels_injected_sweeper(catalog, identity, saga):
    """The sweeper task lives and dies with the app, like the poller."""

    class StubRunner:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def run(self):
            self.started = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    stub = StubRunner()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True),
        catalog=catalog,
        identity=identity,
        saga=saga,
        sweeper=stub,  # type: ignore[arg-type]
    )
    with TestClient(app):
        pass
    assert stub.started and stub.cancelled
