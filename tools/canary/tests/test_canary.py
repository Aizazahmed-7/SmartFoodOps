"""The synthetic customer: every branch of one full product check."""

import asyncio
import json

import httpx
import pytest
from canary.main import Canary
from smartfood_otel import REGISTRY


def _world(overrides=None, confirm_after=1):
    """A scripted gateway: the happy demo world unless a path is overridden.
    The order confirms after `confirm_after` polls."""
    overrides = overrides or {}
    polls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in overrides:
            return overrides[path](request) if callable(overrides[path]) else overrides[path]
        if path == "/v1/auth/login":
            return httpx.Response(200, json={"access_token": "tok"})
        if path == "/v1/me/addresses":
            return httpx.Response(200, json=[{"id": "adr_1"}])
        if path == "/v1/restaurants":
            return httpx.Response(
                200, json={"restaurants": [{"id": "rst_1", "name": "Biryani House"}]}
            )
        if path == "/v1/menus/rst_1":
            return httpx.Response(
                200,
                json={
                    "version": 3,
                    "categories": [
                        {
                            "items": [
                                {
                                    "id": "itm_locked",
                                    "available": True,
                                    "modifier_groups": [{"min_select": 1}],
                                },
                                {"id": "itm_a", "available": True, "modifier_groups": []},
                            ]
                        }
                    ],
                },
            )
        if path == "/v1/orders" and request.method == "POST":
            assert request.headers["Idempotency-Key"].startswith("canary-")
            body = json.loads(request.content)
            assert body["lines"] == [{"item_id": "itm_a", "qty": 1}]
            return httpx.Response(202, json={"order_id": "ord_c", "status": "PLACED"})
        if path == "/v1/orders/ord_c":
            polls["n"] += 1
            status = "CONFIRMED" if polls["n"] >= confirm_after else "PLACED"
            return httpx.Response(200, json={"status": status})
        if path == "/v1/orders/ord_c/cancel":
            return httpx.Response(202, json={"status": "CANCELLING"})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _count(outcome):
    return REGISTRY.get_sample_value("canary_runs_total", {"outcome": outcome}) or 0.0


async def test_happy_run_confirms_cancels_and_stamps_success():
    ok_before = _count("ok")
    canary = Canary("http://gw", _world(confirm_after=2), poll_interval_s=0.0)
    assert await canary.tick() is True
    assert _count("ok") == ok_before + 1
    assert (REGISTRY.get_sample_value("canary_last_success_timestamp_seconds") or 0) > 0


async def test_saga_cancelling_the_order_is_a_failure():
    fail_before = _count("fail")
    world = _world({"/v1/orders/ord_c": httpx.Response(200, json={"status": "CANCELLED"})})
    assert await Canary("http://gw", world).tick() is False
    assert _count("fail") == fail_before + 1


async def test_never_confirming_times_out_as_failure():
    world = _world(confirm_after=999)
    canary = Canary("http://gw", world, poll_deadline_s=0.0, poll_interval_s=0.0)
    assert await canary.run_once() is False


async def test_a_failing_cancel_fails_the_run():
    """The cleanup leg is product too. When /cancel starts 500ing, the canary
    must go red — a green probe there would hide the outage AND leave every
    synthetic order to be compensated by the 180s accept timer, surfacing as
    SystemTimeoutCancels pages that name the wrong thing."""
    world = _world({"/v1/orders/ord_c/cancel": httpx.Response(500, json={})})
    assert await Canary("http://gw", world).run_once() is False


async def test_any_http_error_is_a_failure_not_a_crash():
    world = _world({"/v1/auth/login": httpx.Response(503, json={})})
    assert await Canary("http://gw", world).run_once() is False


async def test_run_loops_until_cancelled():
    canary = Canary("http://gw", _world(), poll_interval_s=0.0)
    task = asyncio.create_task(canary.run(interval_s=0.0))
    before = _count("ok")
    async with asyncio.timeout(2.0):
        # Polling module counter state set by the background loop — there
        # is no event to await, and sleep(0) just yields the loop.
        while _count("ok") < before + 2:  # noqa: ASYNC110
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
