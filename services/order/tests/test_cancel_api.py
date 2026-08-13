"""POST /v1/orders/{id}/cancel end to end: the cancellable window, the
already-done replays, the FR-21 refusals, ownership scoping, and the
SagaGone re-read mapping (workflow finished between our read and the
signal — answer from the truth).

Same discipline as the kitchen suite: orders seeded through the real
placement API, advanced through the real transition() writer against a
FILE-backed sqlite the app shares."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from order.config import Settings
from order.domain.ports import SagaGone, SagaUnavailable
from order.domain.transitions import transition
from order.main import create_app
from smartfood_auth import AuthContext, headers_for
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))
STRANGER = headers_for(AuthContext(sub="usr_2", role="customer"))

TO_CONFIRMED = [
    ("PLACED", "VALIDATED"),
    ("VALIDATED", "PAYMENT_CLEARED"),
    ("PAYMENT_CLEARED", "CONFIRMED"),
]


def snapshot():
    return {
        "restaurant": {
            "id": "rst_1",
            "name": "Biryani House",
            "city": "springfield",
            "status": "open",
            "version": 3,
        },
        "items": [
            {
                "id": "itm_a",
                "name": "Chicken Biryani",
                "price_cents": 1200,
                "currency": "USD",
                "available": True,
                "modifier_groups": [],
            }
        ],
        "missing_item_ids": [],
    }


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path}/order.db"


@pytest.fixture()
def client(catalog, identity, saga, db_url):
    catalog.snapshot = snapshot()
    settings = Settings(database_url=db_url, create_all=True)
    app = create_app(settings, catalog=catalog, identity=identity, saga=saga)
    with TestClient(app) as c:
        yield c


def place(client) -> str:
    r = client.post(
        "/v1/orders",
        json={
            "restaurant_id": "rst_1",
            "menu_version": 3,
            "address_id": "adr_1",
            "card_token": "tok_ok",
            "lines": [{"item_id": "itm_a", "qty": 1}],
        },
        headers={**CUSTOMER, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert r.status_code == 202
    return r.json()["order_id"]


def advance(db_url, order_id, moves, *, reason=None):
    async def _go():
        engine = create_async_engine(db_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        for index, (expected, target) in enumerate(moves):
            await transition(
                sessions,
                order_id,
                expected=expected,
                target=target,
                cancel_reason=reason if index == len(moves) - 1 else None,
            )
        await engine.dispose()

    asyncio.run(_go())


def cancel(client, order_id, headers=CUSTOMER):
    return client.post(f"/v1/orders/{order_id}/cancel", headers=headers)


# ── the cancellable window ─────────────────────────────────────────


def test_cancel_signals_the_saga_from_every_cancellable_state(client, db_url, saga):
    fresh = place(client)  # PLACED — the earliest possible cancel
    r = cancel(client, fresh)
    assert r.status_code == 202
    assert r.json() == {"order_id": fresh, "status": "PLACED"}

    kitchen = place(client)  # deep in the kitchen window
    advance(db_url, kitchen, [*TO_CONFIRMED, ("CONFIRMED", "ACCEPTED"), ("ACCEPTED", "PREPARING")])
    r = cancel(client, kitchen)
    assert r.status_code == 202
    assert r.json()["status"] == "PREPARING"
    assert saga.cancels == [fresh, kitchen]


def test_double_cancel_both_202_flag_is_idempotent(client, db_url, saga):
    order_id = place(client)
    advance(db_url, order_id, TO_CONFIRMED)
    assert cancel(client, order_id).status_code == 202
    assert cancel(client, order_id).status_code == 202  # still CONFIRMED: resignal, harmless
    assert saga.cancels == [order_id, order_id]


# ── already done: 200, never an error ──────────────────────────────


def test_cancelled_family_answers_200_with_the_reason(client, db_url, saga):
    mid_unwind = place(client)
    advance(
        db_url,
        mid_unwind,
        [*TO_CONFIRMED, ("CONFIRMED", "CANCELLING")],
        reason="customer_cancelled",
    )
    r = cancel(client, mid_unwind)
    assert r.status_code == 200
    assert r.json() == {
        "order_id": mid_unwind,
        "status": "CANCELLING",
        "cancel_reason": "customer_cancelled",
    }

    rejected = place(client)  # cancelled by someone ELSE: outcome achieved all the same
    advance(
        db_url,
        rejected,
        [*TO_CONFIRMED, ("CONFIRMED", "CANCELLING"), ("CANCELLING", "CANCELLED")],
        reason="restaurant_rejected",
    )
    r = cancel(client, rejected)
    assert r.status_code == 200
    assert r.json()["cancel_reason"] == "restaurant_rejected"
    assert saga.cancels == []  # nothing signalled in either case


# ── FR-21: the courier has it ──────────────────────────────────────


def test_courier_states_refuse_with_order_not_cancellable(client, db_url, saga):
    for terminal in ("PICKED_UP", "DELIVERED", "SETTLED"):
        order_id = place(client)
        advance(db_url, order_id, [("PLACED", terminal)])
        r = cancel(client, order_id)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ORDER_NOT_CANCELLABLE"
        assert r.json()["error"]["details"] == [
            {"field": "status", "issue": f"order is {terminal}"}
        ]
    assert saga.cancels == []


# ── scoping ────────────────────────────────────────────────────────


def test_cancel_is_ownership_scoped(client, db_url, saga):
    order_id = place(client)
    advance(db_url, order_id, TO_CONFIRMED)
    assert cancel(client, order_id, headers=STRANGER).status_code == 404
    assert cancel(client, "ord_ghost").status_code == 404
    assert saga.cancels == []


# ── the SagaGone re-read mapping ───────────────────────────────────


def _mutating_signal(db_url, order_id, moves, *, reason):
    """A signal_cancel stand-in: the workflow finishes (moving the row)
    just before our signal lands — then reports itself gone."""

    async def gone(_order_id):
        engine = create_async_engine(db_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        for index, (expected, target) in enumerate(moves):
            await transition(
                sessions,
                _order_id,
                expected=expected,
                target=target,
                cancel_reason=reason if index == len(moves) - 1 else None,
            )
        await engine.dispose()
        raise SagaGone(f"ord::{_order_id}")

    return gone


def test_saga_gone_rereads_cancelled_as_200(client, db_url, saga):
    order_id = place(client)
    advance(db_url, order_id, TO_CONFIRMED)
    saga.signal_cancel = _mutating_signal(
        db_url,
        order_id,
        [("CONFIRMED", "CANCELLING"), ("CANCELLING", "CANCELLED")],
        reason="restaurant_timeout",
    )
    r = cancel(client, order_id)
    assert r.status_code == 200  # the timer got there first — outcome achieved
    assert r.json()["cancel_reason"] == "restaurant_timeout"


def test_saga_gone_rereads_courier_state_as_409(client, db_url, saga):
    order_id = place(client)
    advance(db_url, order_id, TO_CONFIRMED)
    saga.signal_cancel = _mutating_signal(db_url, order_id, [("CONFIRMED", "SETTLED")], reason=None)
    r = cancel(client, order_id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_NOT_CANCELLABLE"


def test_saga_gone_with_no_workflow_yet_is_503(client, saga):
    """The placement→start gap: still-cancellable status but nothing is
    listening — the only honest answer is retry."""
    order_id = place(client)  # PLACED, RecordingSaga never started a workflow
    saga.fail_with = SagaGone(f"ord::{order_id}")
    r = cancel(client, order_id)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["retry-after"] == "1"


def test_temporal_down_is_503(client, db_url, saga):
    order_id = place(client)
    advance(db_url, order_id, TO_CONFIRMED)
    saga.fail_with = SagaUnavailable("boom")
    assert cancel(client, order_id).status_code == 503
