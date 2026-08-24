"""S9 bell push: per-recipient tickets from the verified identity, the
claim-scoped stream (no identity in the URL), cross-lane ticket refusal,
post-commit hints from the inbox handler, and the fail-open seam."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient
from notification import push
from notification.config import Settings
from notification.main import create_app
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))
OWNER = headers_for(AuthContext(sub="usr_o", role="restaurant_admin", restaurant_id="rst_1"))


class FakeRealtime:
    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.buses: dict[str, asyncio.Queue] = {}
        self.published: list[tuple[str, str]] = []

    async def put_ticket(self, ticket, channel, sub, *, ttl_s):
        self.tickets[ticket] = {"channel": channel, "sub": sub, "ttl": ttl_s}

    async def consume_ticket(self, ticket):
        return self.tickets.pop(ticket, None)

    async def publish(self, channel, data):
        self.published.append((channel, data))
        self.buses.setdefault(channel, asyncio.Queue()).put_nowait(data)

    @asynccontextmanager
    async def subscription(self, channel):
        queue = self.buses.setdefault(channel, asyncio.Queue())

        class Sub:
            async def next_message(self):
                try:
                    return await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    return None

        yield Sub()


def make_app(fake, **knobs):
    return create_app(
        Settings(
            database_url="sqlite+aiosqlite://",
            create_all=True,
            stream_heartbeat_seconds=knobs.pop("hb", 0.05),
            stream_lifetime_min_seconds=knobs.pop("life", 0.4),
            stream_lifetime_max_seconds=knobs.pop("life_max", 0.4),
        ),
        realtime=fake,  # type: ignore[arg-type]
    )


def test_ticket_names_the_callers_own_channel_only():
    fake = FakeRealtime()
    with TestClient(make_app(fake)) as c:
        body = c.post("/v1/notifications/ticket", headers=CUSTOMER).json()
        assert body["stream"] == "/sse/notify"
        claim = fake.tickets[body["ticket"]]
        assert claim["channel"] == "sfo:notify:customer:usr_1"
        owner_body = c.post("/v1/notifications/ticket", headers=OWNER).json()
        assert fake.tickets[owner_body["ticket"]]["channel"] == "sfo:notify:restaurant:rst_1"


def test_ticket_503s_when_push_is_off():
    with TestClient(make_app(None)) as c:
        r = c.post("/v1/notifications/ticket", headers=CUSTOMER)
        assert r.status_code == 503 and r.headers["Retry-After"] == "30"
        assert c.get("/v1/notifications/stream?ticket=x").status_code == 503


def test_stream_rejects_spent_missing_and_foreign_lane_tickets():
    fake = FakeRealtime()
    app = make_app(fake)
    with TestClient(app) as c:
        ticket = c.post("/v1/notifications/ticket", headers=CUSTOMER).json()["ticket"]
        # a TRACKING ticket redeemed at the bell: burned AND refused
        asyncio.run(fake.put_ticket("trk", "sfo:track:ord_1", "usr_1", ttl_s=60))
        assert c.get("/v1/notifications/stream?ticket=trk").status_code == 401
        assert "trk" not in fake.tickets  # burned
        assert c.get("/v1/notifications/stream?ticket=never-sold").status_code == 401
        # the legitimate ticket still works exactly once
        with c.stream("GET", f"/v1/notifications/stream?ticket={ticket}") as r:
            assert r.status_code == 200
        assert c.get(f"/v1/notifications/stream?ticket={ticket}").status_code == 401


async def test_stream_relays_hints_and_heartbeats():
    fake = FakeRealtime()
    app = make_app(fake, hb=0.02)
    await fake.put_ticket("tkt", "sfo:notify:customer:usr_1", "usr_1", ttl_s=60)
    await fake.publish("sfo:notify:customer:usr_1", "customer")

    transport = httpx.ASGITransport(app=app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("GET", "/v1/notifications/stream?ticket=tkt") as response:
            assert response.status_code == 200
            async with asyncio.timeout(3.0):
                async for line in response.aiter_lines():
                    lines.append(line)
                    if "reconnect" in line:
                        break
    assert "data: customer" in lines  # the hint relayed
    assert ": hb" in lines  # quiet stretch heartbeat
    assert "event: reconnect" in lines  # jittered lifetime ended it


# ── the handler hook ────────────────────────────────────────────────


async def test_inbox_handler_hints_each_distinct_recipient_post_commit():
    from notification.consumers import InboxHandler
    from notification.db import metadata
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    fake = FakeRealtime()
    push.set_publisher(fake)
    try:
        await InboxHandler(sessions).handle(
            {
                "event_id": "evt_1",
                "event_type": "OrderConfirmed",
                "aggregate_type": "order",
                "aggregate_id": "ord_1",
                "occurred_at": "2026-08-25T10:00:00+00:00",
                "payload": json.dumps(
                    {
                        "order_id": "ord_1",
                        "user_id": "usr_1",
                        "restaurant_id": "rst_1",
                        "restaurant_name": "Biryani House",
                        "items": [],
                        "totals": {"total_cents": 1000, "currency": "USD"},
                        "status": "CONFIRMED",
                    }
                ),
            }
        )
    finally:
        push.reset_publisher()
    channels = sorted(c for c, _ in fake.published)
    assert channels == ["sfo:notify:customer:usr_1", "sfo:notify:restaurant:rst_1"]


async def test_hint_failure_never_breaks_the_handler():
    class Exploding:
        async def publish(self, channel, data):
            raise RuntimeError("bus down")

    push.set_publisher(Exploding())
    try:
        await push.publish_hint("customer", "usr_1")  # must not raise
    finally:
        push.reset_publisher()


async def test_unarmed_push_is_a_noop():
    push.reset_publisher()
    await push.publish_hint("customer", "usr_1")
