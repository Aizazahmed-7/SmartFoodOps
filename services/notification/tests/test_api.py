"""The inbox surface through the real app: recipient resolution from the
edge-stamped identity, keyset paging, unread counts, idempotent read-marking,
ownership-in-the-WHERE 404s.

Harness note: rows only ever arrive via the consumer, so each test seeds by
running the REAL InboxHandler against the same sessionmaker the app's
service reads — one event loop end to end (httpx ASGI, no TestClient portal).
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from notification.config import Settings
from notification.consumers import InboxHandler
from notification.db import metadata
from notification.domain.service import NotificationService
from notification.main import create_app
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

CUSTOMER = {"X-Auth-Sub": "usr_1", "X-Auth-Role": "customer"}
OTHER_CUSTOMER = {"X-Auth-Sub": "usr_2", "X-Auth-Role": "customer"}
PARTNER = {
    "X-Auth-Sub": "usr_owner",
    "X-Auth-Role": "restaurant_admin",
    "X-Auth-Restaurant-Id": "rst_1",
}
RIDER = {"X-Auth-Sub": "usr_r", "X-Auth-Role": "rider"}

BASE = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


@pytest.fixture()
async def harness():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))
    app.state.service = NotificationService(sessions)  # the test owns the DB
    handler = InboxHandler(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://notification.test"
    ) as client:
        yield client, handler


def _order_event(event_id: str, *, minutes: int = 0, event_type: str = "OrderConfirmed"):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": "order",
        "aggregate_id": f"ord_{event_id}",
        "aggregate_version": 1,
        "occurred_at": BASE + timedelta(minutes=minutes),
        "cell_id": "c1",
        "payload": json.dumps(
            {
                "order_id": f"ord_{event_id}",
                "user_id": "usr_1",
                "restaurant_id": "rst_1",
                "restaurant_name": "Biryani House",
                "status": "CONFIRMED",
                "items": [{"menu_item_id": "itm_1", "qty": 1}],
                "totals": {"total_cents": 1200, "currency": "USD"},
                "cancel_reason": None,
            }
        ),
    }


async def _seed_confirmed(handler: InboxHandler, n: int = 1) -> None:
    """n confirmations → n unread rows for usr_1 AND n for rst_1."""
    for i in range(n):
        await handler.handle(_order_event(f"evt-{i}", minutes=i))


async def test_customer_reads_their_own_inbox(harness):
    client, handler = harness
    await _seed_confirmed(handler)
    body = (await client.get("/v1/notifications", headers=CUSTOMER)).json()
    assert body["unread"] == 1
    (item,) = body["items"]
    assert item["title"] == "Order confirmed"
    assert item["read_at"] is None and body["next_cursor"] is None


async def test_partner_reads_the_restaurants_inbox(harness):
    client, handler = harness
    await _seed_confirmed(handler)
    (item,) = (await client.get("/v1/notifications", headers=PARTNER)).json()["items"]
    assert item["title"] == "New order to accept"


async def test_inboxes_are_disjoint(harness):
    client, handler = harness
    await _seed_confirmed(handler)
    body = (await client.get("/v1/notifications", headers=OTHER_CUSTOMER)).json()
    assert body == {"items": [], "next_cursor": None, "unread": 0}


async def test_keyset_pagination_walks_newest_first(harness):
    client, handler = harness
    await _seed_confirmed(handler, n=3)
    first = (await client.get("/v1/notifications?limit=2", headers=CUSTOMER)).json()
    assert [i["order_id"] for i in first["items"]] == ["ord_evt-2", "ord_evt-1"]
    assert first["next_cursor"] is not None
    second = (
        await client.get(
            f"/v1/notifications?limit=2&cursor={first['next_cursor']}", headers=CUSTOMER
        )
    ).json()
    assert [i["order_id"] for i in second["items"]] == ["ord_evt-0"]
    assert second["next_cursor"] is None


async def test_malformed_cursor_is_422(harness):
    client, _ = harness
    resp = await client.get("/v1/notifications?cursor=garbage", headers=CUSTOMER)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_decodable_but_wrong_shape_cursor_is_still_422(harness):
    """Valid base64 of the wrong JSON shape raises TypeError internally —
    the contract says ValueError→422, never a 500."""
    import base64

    client, _ = harness
    for wrong in (b'"5"', b"[]", b'[123, "x"]'):
        cursor = base64.urlsafe_b64encode(wrong).decode()
        resp = await client.get(f"/v1/notifications?cursor={cursor}", headers=CUSTOMER)
        assert resp.status_code == 422, wrong
        assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_same_instant_rows_page_without_loss(harness):
    """The keyset tiebreaker: two notifications minted at the SAME
    occurred_at must both surface across cursor pages — id DESC breaks
    the tie, and a timestamp-only cursor would drop or repeat one."""
    client, handler = harness
    await handler.handle(_order_event("evt-a"))
    await handler.handle(_order_event("evt-b"))  # same minutes=0 timestamp
    seen: list[str] = []
    cursor = ""
    for _ in range(3):
        url = f"/v1/notifications?limit=1{f'&cursor={cursor}' if cursor else ''}"
        page = (await client.get(url, headers=CUSTOMER)).json()
        seen += [i["id"] for i in page["items"]]
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert len(seen) == 2 and len(set(seen)) == 2  # no loss, no repeats


async def test_mark_read_is_idempotent_and_clears_unread(harness):
    client, handler = harness
    await _seed_confirmed(handler)
    (item,) = (await client.get("/v1/notifications", headers=CUSTOMER)).json()["items"]
    first = (await client.post(f"/v1/notifications/{item['id']}/read", headers=CUSTOMER)).json()
    assert first["read_at"] is not None
    second = (await client.post(f"/v1/notifications/{item['id']}/read", headers=CUSTOMER)).json()
    assert second["read_at"] == first["read_at"]  # original timestamp survives
    assert (await client.get("/v1/notifications", headers=CUSTOMER)).json()["unread"] == 0


async def test_marking_someone_elses_notification_is_404(harness):
    client, handler = harness
    await _seed_confirmed(handler)
    (item,) = (await client.get("/v1/notifications", headers=CUSTOMER)).json()["items"]
    resp = await client.post(f"/v1/notifications/{item['id']}/read", headers=OTHER_CUSTOMER)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    # And the rightful owner's row is untouched.
    assert (await client.get("/v1/notifications", headers=CUSTOMER)).json()["unread"] == 1


async def test_read_all_marks_only_this_recipient(harness):
    client, handler = harness
    await _seed_confirmed(handler, n=2)
    marked = (await client.post("/v1/notifications/read-all", headers=CUSTOMER)).json()
    assert marked == {"marked": 2}
    assert (await client.get("/v1/notifications", headers=CUSTOMER)).json()["unread"] == 0
    # The restaurant's copies of the same events are still unread.
    assert (await client.get("/v1/notifications", headers=PARTNER)).json()["unread"] == 2
    again = (await client.post("/v1/notifications/read-all", headers=CUSTOMER)).json()
    assert again == {"marked": 0}


async def test_roles_without_an_inbox_are_forbidden(harness):
    client, _ = harness
    resp = await client.get("/v1/notifications", headers=RIDER)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN_ROLE"


async def test_missing_identity_is_401(harness):
    client, _ = harness
    assert (await client.get("/v1/notifications")).status_code == 401


async def test_the_apps_own_wiring_end_to_end():
    """No service swap: the app's OWN engine, lifespan create_all, and
    composition root serve a real request after the real handler wrote
    through the same sessionmaker the app built."""
    app = create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))
    async with app.router.lifespan_context(app):  # one loop end to end
        await InboxHandler(app.state.service._sessions).handle(_order_event("evt-own"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://notification.test"
        ) as client:
            body = (await client.get("/v1/notifications", headers=CUSTOMER)).json()
    assert body["unread"] == 1
    assert body["items"][0]["order_id"] == "ord_evt-own"
