"""S4 live tracking: ticket auth (single-use, ownership), the SSE stream
(snapshot, hints, heartbeats, jittered lifetime, terminal close), the
post-commit publish hooks, and the fail-open publisher seam."""

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient
from order import tracking
from order.config import Settings
from order.main import create_app
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))
STRANGER = headers_for(AuthContext(sub="usr_2", role="customer"))


class FakeTracking:
    """The TrackingPort, in-memory: dict tickets, one asyncio.Queue bus per
    order. next_status() mimics the Redis adapter's poll-tick contract
    (None on a quiet tick)."""

    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.buses: dict[str, asyncio.Queue] = {}
        self.published: list[tuple[str, str]] = []

    async def put_ticket(self, ticket, order_id, sub, *, ttl_s):
        self.tickets[ticket] = {"order_id": order_id, "sub": sub, "ttl": ttl_s}

    async def consume_ticket(self, ticket):
        return self.tickets.pop(ticket, None)

    async def publish(self, order_id, status):
        self.published.append((order_id, status))
        self.buses.setdefault(order_id, asyncio.Queue()).put_nowait(status)

    @asynccontextmanager
    async def subscription(self, order_id):
        queue = self.buses.setdefault(order_id, asyncio.Queue())

        class Sub:
            async def next_status(self):
                # Block like the real adapter (get_message timeout=1.0):
                # a quiet bus must let the stream's own heartbeat timeout
                # fire, not busy-spin past it.
                try:
                    return await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    return None

        yield Sub()


def make_app(fake: FakeTracking | None, **knobs):
    return create_app(
        Settings(
            database_url="sqlite+aiosqlite://",
            create_all=True,
            track_ticket_ttl_seconds=knobs.pop("ttl", 60),
            track_heartbeat_seconds=knobs.pop("hb", 0.05),
            track_lifetime_min_seconds=knobs.pop("life", 30.0),
            track_lifetime_max_seconds=knobs.pop("life_max", 30.0),
        ),
        tracking_port=fake,
    )


async def _seed(app, order_id="ord_t1", user="usr_1", status="CONFIRMED"):
    from datetime import UTC, datetime

    from order.db import metadata, orders

    # ASGITransport never runs the lifespan, so the async streaming tests
    # create the schema here (idempotent; the TestClient tests get it from
    # the lifespan's create_all as usual).
    async with app.state.sessions() as schema_session:
        conn = await schema_session.connection()
        await conn.run_sync(metadata.create_all)
        await schema_session.commit()

    async def put():
        async with app.state.sessions() as s:
            await s.execute(
                orders.insert().values(
                    order_id=order_id,
                    user_id=user,
                    restaurant_id="rst_1",
                    restaurant_name_snapshot="Biryani House",
                    status=status,
                    aggregate_version=3,
                    payment_method="CARD",
                    card_token="tok_ok",
                    menu_version=1,
                    pricing_snapshot={"currency": "USD", "total_cents": 1000},
                    delivery_address_snapshot={},
                    request_hash="h",
                    placed_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await s.commit()

    await put()


def _seed_order(app, **kw):
    asyncio.run(_seed(app, **kw))


# ── tickets ─────────────────────────────────────────────────────────


def test_ticket_requires_ownership():
    fake = FakeTracking()
    app = make_app(fake)
    with TestClient(app) as c:
        _seed_order(app)
        yours = c.post("/v1/track/ticket", json={"order_id": "ord_t1"}, headers=CUSTOMER)
        assert yours.status_code == 201
        body = yours.json()
        assert body["stream"] == "/sse/track/ord_t1" and body["expires_in"] == 60
        not_yours = c.post("/v1/track/ticket", json={"order_id": "ord_t1"}, headers=STRANGER)
        assert not_yours.status_code == 404  # not-found and not-yours: one shape


def test_ticket_endpoint_503s_when_tracking_is_off():
    """No Redis configured → the FE keeps polling. 503 + Retry-After is the
    honest shape; a 500 would page somebody for a feature toggle."""
    app = make_app(None)
    with TestClient(app) as c:
        _seed_order(app)
        r = c.post("/v1/track/ticket", json={"order_id": "ord_t1"}, headers=CUSTOMER)
        assert r.status_code == 503 and r.headers["Retry-After"] == "30"
        assert c.get("/v1/track/ord_t1?ticket=x").status_code == 503


async def _stream_lines(app, path, *, max_lines=10, deadline=2.0):
    transport = httpx.ASGITransport(app=app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("GET", path) as response:
            if response.status_code != 200:
                return response.status_code, []
            async with asyncio.timeout(deadline):
                async for line in response.aiter_lines():
                    lines.append(line)
                    if len([ln for ln in lines if ln]) >= max_lines:
                        break
    return 200, lines


def test_stream_rejects_missing_spent_and_mismatched_tickets():
    fake = FakeTracking()
    app = make_app(fake)
    with TestClient(app) as c:
        _seed_order(app)
        ticket = c.post("/v1/track/ticket", json={"order_id": "ord_t1"}, headers=CUSTOMER).json()[
            "ticket"
        ]
        # mismatch burns the ticket…
        assert c.get(f"/v1/track/ord_OTHER?ticket={ticket}").status_code == 401
        # …so the legitimate order can no longer use it either (spent).
        assert c.get(f"/v1/track/ord_t1?ticket={ticket}").status_code == 401
        assert c.get("/v1/track/ord_t1?ticket=never-issued").status_code == 401


async def test_stream_snapshot_then_hint_then_terminal_close():
    fake = FakeTracking()
    app = make_app(fake)
    await _seed(app, status="CONFIRMED")
    await fake.put_ticket("tkt", "ord_t1", "usr_1", ttl_s=60)
    await fake.publish("ord_t1", "ACCEPTED")  # queued before connect
    await fake.publish("ord_t1", "SETTLED")  # terminal — must end the stream

    status, lines = await _stream_lines(app, "/v1/track/ord_t1?ticket=tkt")
    assert status == 200
    data = [ln for ln in lines if ln.startswith("data: ")]
    assert data == ["data: CONFIRMED", "data: ACCEPTED", "data: SETTLED"]
    # the generator RETURNED after SETTLED — reaching here proves the close


async def test_stream_on_an_already_terminal_order_is_one_event():
    fake = FakeTracking()
    app = make_app(fake)
    await _seed(app, status="CANCELLED")
    await fake.put_ticket("tkt", "ord_t1", "usr_1", ttl_s=60)
    status, lines = await _stream_lines(app, "/v1/track/ord_t1?ticket=tkt")
    assert status == 200
    assert [ln for ln in lines if ln.startswith("data: ")] == ["data: CANCELLED"]


async def test_quiet_stream_heartbeats():
    fake = FakeTracking()
    # Short lifetime too: ASGITransport does not cancel the generator on an
    # early client break, so the test would otherwise drain the default.
    app = make_app(fake, hb=0.02, life=0.3, life_max=0.3)
    await _seed(app, status="CONFIRMED")
    await fake.put_ticket("tkt", "ord_t1", "usr_1", ttl_s=60)
    status, lines = await _stream_lines(app, "/v1/track/ord_t1?ticket=tkt", max_lines=3)
    assert status == 200
    assert ": hb" in lines  # SSE comment — invisible to EventSource handlers


async def test_lifetime_bound_sends_reconnect_and_closes():
    """FR-36's jitter: the injected rng pins the lifetime tiny; the stream
    must announce `reconnect` (fresh-ticket handshake) rather than EOFing."""
    fake = FakeTracking()
    app = make_app(fake, life=0.03, life_max=0.03, hb=0.5)
    await _seed(app, status="CONFIRMED")
    await fake.put_ticket("tkt", "ord_t1", "usr_1", ttl_s=60)
    status, lines = await _stream_lines(app, "/v1/track/ord_t1?ticket=tkt")
    assert status == 200
    assert "event: reconnect" in lines and "data: lifetime" in lines


# ── the publish hooks ───────────────────────────────────────────────


async def test_transition_publishes_after_commit():
    from order.db import metadata
    from order.domain.transitions import transition
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    fake = FakeTracking()
    tracking.set_publisher(fake)
    try:
        from datetime import UTC, datetime

        from order.db import orders

        async with sessions() as s:
            await s.execute(
                orders.insert().values(
                    order_id="ord_h1",
                    user_id="u",
                    restaurant_id="r",
                    restaurant_name_snapshot="n",
                    status="PLACED",
                    aggregate_version=0,
                    payment_method="CARD",
                    card_token="t",
                    menu_version=1,
                    pricing_snapshot={},
                    delivery_address_snapshot={},
                    request_hash="h",
                    placed_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await s.commit()

        await transition(sessions, "ord_h1", expected="PLACED", target="VALIDATED")
        assert fake.published == [("ord_h1", "VALIDATED")]

        # idempotent no-op (already VALIDATED) publishes NOTHING — a hint
        # for a write that did not happen would be a small lie
        await transition(sessions, "ord_h1", expected="PLACED", target="VALIDATED")
        assert len(fake.published) == 1
    finally:
        tracking.reset_publisher()


async def test_publish_failure_never_breaks_the_transition():
    class ExplodingPublisher:
        async def publish(self, order_id, status):
            raise RuntimeError("bus down")

    tracking.set_publisher(ExplodingPublisher())
    try:
        await tracking.publish_status("ord_x", "CONFIRMED")  # must not raise
    finally:
        tracking.reset_publisher()


async def test_unarmed_publisher_is_a_noop():
    tracking.reset_publisher()
    await tracking.publish_status("ord_x", "CONFIRMED")  # nothing, silently


async def test_quiet_bus_tick_continues_without_heartbeat():
    """The bus poll returning None (its own internal timeout) inside a
    LONGER heartbeat window: the loop continues silently — a None tick is
    not an event and must not fake a heartbeat either."""
    fake = FakeTracking()
    app = make_app(fake, hb=5.0, life=1.4, life_max=1.4)
    await _seed(app, status="CONFIRMED")
    await fake.put_ticket("tkt", "ord_t1", "usr_1", ttl_s=60)
    status, lines = await _stream_lines(app, "/v1/track/ord_t1?ticket=tkt", deadline=4.0)
    assert status == 200
    # The bus's 1.0s internal tick returned None INSIDE the 5s heartbeat
    # window (the branch under test); the lifetime then ended the stream.
    # One boundary heartbeat may fire when remaining < heartbeat_s —
    # min(hb, remaining) — a spare comment line, not a bug.
    assert lines.count(": hb") <= 1
    assert "event: reconnect" in lines
