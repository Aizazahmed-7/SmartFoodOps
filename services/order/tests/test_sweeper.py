"""The placement sweeper: only stale PLACED orders get sagas, heals are
only claimed when a workflow was actually STARTED, per-order failures never
shadow the backlog, and the loop lives/dies with the app."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from order.adapters.repo import OrderRepo
from order.config import Settings
from order.db import metadata, orders
from order.main import create_app
from order.sweeper import Sweeper
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _sweeper(sessions, saga, **kwargs) -> Sweeper:
    kwargs.setdefault("min_age_seconds", 60)
    return Sweeper(sessions, saga, now=lambda: NOW, **kwargs)


class RecordingSaga:
    """SagaStarter fake: records every attempt; `existing` ids report an
    already-started workflow (False); `fail_for` ids raise."""

    def __init__(self, fail_for: set[str] | None = None, existing: set[str] | None = None):
        self.calls: list[str] = []
        self.started: list[str] = []
        self.fail_for = fail_for or set()
        self.existing = existing or set()

    async def start(self, order_id: str) -> bool:
        self.calls.append(order_id)
        if order_id in self.fail_for:
            raise RuntimeError("temporal unreachable")
        if order_id in self.existing:
            return False
        self.started.append(order_id)
        return True


async def _harness():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _insert_order(sessions, order_id: str, *, status: str, age_seconds: float) -> None:
    placed_at = NOW - timedelta(seconds=age_seconds)
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
    swept = await _sweeper(sessions, saga).sweep_once()
    assert swept == 1
    assert saga.started == ["ord_stale"]


async def test_exact_min_age_boundary_is_not_yet_stale():
    """placed_at == cutoff must NOT sweep: the scan is strictly-older-than,
    so an order becomes sweepable only once it has fully aged past min_age."""
    sessions = await _harness()
    await _insert_order(sessions, "ord_edge", status="PLACED", age_seconds=60)
    saga = RecordingSaga()
    assert await _sweeper(sessions, saga, min_age_seconds=60).sweep_once() == 0
    assert saga.calls == []


async def test_oldest_debts_are_paid_first():
    sessions = await _harness()
    # Drop the partial index so ordering can only come from the repo's
    # ORDER BY — with the index present, sqlite happens to return index
    # order and a dropped ORDER BY would pass incidentally.
    async with sessions() as session:
        await session.execute(sa.text("DROP INDEX ix_orders_sweeper"))
        await session.commit()
    await _insert_order(sessions, "ord_newer", status="PLACED", age_seconds=100)
    await _insert_order(sessions, "ord_older", status="PLACED", age_seconds=300)
    saga = RecordingSaga()
    await _sweeper(sessions, saga).sweep_once()
    assert saga.started == ["ord_older", "ord_newer"]


async def test_already_existing_workflow_is_not_claimed_as_a_heal():
    """start() returning False = the workflow exists (running, or CLOSED
    and permanently unstartable) — the sweeper did nothing and must not
    count or log a heal it didn't perform."""
    sessions = await _harness()
    await _insert_order(sessions, "ord_alive", status="PLACED", age_seconds=300)
    await _insert_order(sessions, "ord_dead", status="PLACED", age_seconds=200)
    saga = RecordingSaga(existing={"ord_alive", "ord_dead"})
    swept = await _sweeper(sessions, saga).sweep_once()
    assert swept == 0
    assert saga.calls == ["ord_alive", "ord_dead"] and saga.started == []


async def test_one_failed_start_never_shadows_the_rest():
    """Temporal hiccup on one order: log, skip, keep sweeping — the row
    stays PLACED so the next pass retries it."""
    sessions = await _harness()
    await _insert_order(sessions, "ord_bad", status="PLACED", age_seconds=300)
    await _insert_order(sessions, "ord_good", status="PLACED", age_seconds=200)
    saga = RecordingSaga(fail_for={"ord_bad"})
    swept = await _sweeper(sessions, saga).sweep_once()
    assert swept == 1
    assert saga.started == ["ord_good"]
    # Next pass, Temporal recovered: the skipped order is still owed.
    saga.fail_for = set()
    assert await _sweeper(sessions, saga).sweep_once() == 2


async def test_empty_backlog_sweeps_nothing():
    sessions = await _harness()
    saga = RecordingSaga()
    assert await _sweeper(sessions, saga).sweep_once() == 0
    assert saga.calls == []


async def test_partial_index_is_pinned():
    """ix_orders_sweeper and its PLACED predicate must exist — deleting it
    from db.py would silently turn every sweep into a full-table scan."""
    sessions = await _harness()
    async with sessions() as session:
        row = (
            await session.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name = 'ix_orders_sweeper'")
            )
        ).one()
    assert "PLACED" in row.sql


async def test_run_survives_a_failing_pass_and_cancels_cleanly():
    """The reaper/poller contract: a bad pass (DB down here) logs and
    retries; cancellation is the shutdown signal."""

    class ExplodingSessions:
        calls = 0

        def __call__(self):
            type(self).calls += 1
            raise RuntimeError("db down")

    sweeper = Sweeper(ExplodingSessions(), RecordingSaga(), interval_seconds=0.0)  # type: ignore[arg-type]
    task = asyncio.create_task(sweeper.run())
    async with asyncio.timeout(2.0):
        # Polling stub state set by a background task — nothing to await.
        while ExplodingSessions.calls < 2:  # noqa: ASYNC110
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ExplodingSessions.calls >= 2  # crashed pass did not kill the loop


async def test_mid_sweep_cancellation_propagates():
    """Cancellation landing INSIDE a sweep (not between them) must still
    end the loop — the run() re-raise, exercised deterministically."""

    class BlockingSessions:
        def __init__(self):
            self.entered = asyncio.Event()

        def __call__(self):
            return self

        async def __aenter__(self):
            self.entered.set()
            await asyncio.sleep(3600)

        async def __aexit__(self, *exc: object) -> bool:
            return False

    sessions = BlockingSessions()
    task = asyncio.create_task(Sweeper(sessions, RecordingSaga()).run())  # type: ignore[arg-type]
    async with asyncio.timeout(2.0):
        await sessions.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_default_wiring_sweeps_for_real(tmp_path, catalog, identity, saga):
    """No injected sweeper: create_app builds one from Settings and the
    lifespan runs it — a backdated PLACED row gets its saga through the
    app's OWN wiring, proving the default construction end to end."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/sweep.db"
    app = create_app(
        Settings(
            database_url=db_url,
            create_all=True,
            sweeper_interval_seconds=0.05,
            sweeper_min_age_seconds=60,
        ),
        catalog=catalog,
        identity=identity,
        saga=saga,
    )
    async with app.router.lifespan_context(app):
        engine = create_async_engine(db_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            placed_at = datetime.now(UTC) - timedelta(seconds=300)
            await OrderRepo(session).insert_order(
                order_id="ord_wired",
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
            await session.commit()
        async with asyncio.timeout(3.0):
            # Polling a fake fed by the app's own background task.
            while "ord_wired" not in saga.started:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        await engine.dispose()


def test_lifespan_runs_and_cancels_injected_sweeper(catalog, identity, saga):
    """The sweeper task lives and dies with the app, like the poller —
    and interval 0 means the task is never started at all."""

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

    disabled = StubRunner()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True, sweeper_interval_seconds=0),
        catalog=catalog,
        identity=identity,
        saga=saga,
        sweeper=disabled,  # type: ignore[arg-type]
    )
    with TestClient(app):
        pass
    assert not disabled.started  # interval 0 = the gate held it back
