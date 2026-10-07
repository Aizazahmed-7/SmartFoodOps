"""The kitchen-congestion read, over httpx.MockTransport (FR-85).

Every branch returns either a fact or None, and the tests worth having are
the ones that prove None never becomes a number. A congestion clause is an
optional sentence in an explanation; the customer's answer is not.
"""

from datetime import UTC, datetime

import httpx
import pytest
from ai_assistant.adapters.inventory_client import InventoryClient

AS_OF = "2026-09-28T12:00:00+00:00"


def _client(handler, **kwargs) -> InventoryClient:
    return InventoryClient(
        "http://inventory:8005",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_delay=0.0,
        **kwargs,
    )


async def test_a_load_reading_carries_inventorys_own_clock():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/internal/restaurants/rst_1/load"
        # Internal trust (ADR-0005): the endpoint is SystemOnly, so the
        # call only succeeds with the service identity attached, and the
        # caller named for audit.
        assert request.headers["x-auth-roles"] == "system"
        assert request.headers["x-internal-caller"] == "ai-assistant"
        return httpx.Response(
            200, json={"restaurant_id": "rst_1", "active": 7, "capacity": 8, "as_of": AS_OF}
        )

    load = await _client(handler).load("rst_1")
    assert load is not None
    assert (load.active, load.capacity) == (7, 8)
    assert load.as_of == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


async def test_active_above_capacity_is_reported_not_clamped():
    """A kitchen holding 5 with capacity 2 is a real, legal state — the
    owner lowered capacity while orders were running. It is also exactly
    the state most worth explaining, so nothing may normalise it away."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"restaurant_id": "rst_1", "active": 5, "capacity": 2, "as_of": AS_OF}
        )

    load = await _client(handler).load("rst_1")
    assert load is not None
    assert (load.active, load.capacity) == (5, 2)


async def test_a_kitchen_with_no_load_row_reads_as_no_fact():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    assert await _client(handler).load("rst_ghost") is None


async def test_a_refusal_costs_the_congestion_clause_not_the_answer():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "FORBIDDEN"}})

    assert await _client(handler).load("rst_1") is None


async def test_a_flapping_inventory_is_retried_then_given_up_on():
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    assert await _client(handler).load("rst_1") is None
    assert len(calls) == 2  # two attempts: a render path, not a money path


async def test_a_transport_error_on_the_first_attempt_heals_on_the_second():
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("inventory is restarting")
        return httpx.Response(
            200, json={"restaurant_id": "rst_1", "active": 1, "capacity": 4, "as_of": AS_OF}
        )

    load = await _client(handler).load("rst_1")
    assert load is not None and load.active == 1


async def test_a_total_outage_is_no_fact_rather_than_an_exception():
    """Raising would fail the whole explanation because one contributing
    fact is unavailable — turning a partial answer into no answer."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("inventory is gone")

    assert await _client(handler).load("rst_1") is None


@pytest.mark.parametrize(
    "body",
    [
        {"active": 3},  # capacity missing
        {"active": "many", "capacity": 8},  # not a number
        {"active": None, "capacity": 8},
        ["not", "an", "object"],
    ],
)
async def test_a_body_we_cannot_read_is_no_fact(body):
    """Contract drift must cost the congestion clause, never a 500. This
    should be unreachable — Inventory is first-party — which is precisely
    why it must not be an exception."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    assert await _client(handler).load("rst_1") is None


async def test_a_missing_or_unparseable_clock_falls_back_to_ours():
    """A reading with no timestamp cannot be aged by the caller, and the
    caller is about to write the words "right now"."""
    before = datetime.now(UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"active": 2, "capacity": 6, "as_of": "not-a-date"})

    load = await _client(handler).load("rst_1")
    assert load is not None and load.as_of >= before
