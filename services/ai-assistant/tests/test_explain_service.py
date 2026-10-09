"""The explanation, assembled from three services (FR-83, FR-86).

The resolver and the renderer are tested exhaustively elsewhere. What is
tested here is everything that can go wrong between them and the network:
which missing fact is fatal, which merely costs a clause, and who is
allowed to ask.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ai_assistant.adapters.inventory_client import InventoryClient
from ai_assistant.adapters.order_client import (
    DeliveryClient,
    OrderTimelineClient,
)
from ai_assistant.domain.explain import ReasonCode
from ai_assistant.domain.ports import UpstreamUnavailable
from ai_assistant.explain_service import ExplainService, NotYours

NOW = datetime.now(UTC)


def _iso(delta_s: int) -> str:
    return (NOW - timedelta(seconds=delta_s)).isoformat()


def _timeline_body(**overrides):
    body = {
        "order_id": "ord_1",
        "user_id": "usr_1",
        "restaurant_id": "rst_1",
        "status": "READY",
        "cancel_reason": None,
        "placed_at": _iso(1800),
        "confirmed_at": _iso(1700),
        "accepted_at": _iso(1600),
        "preparing_at": _iso(1500),
        "ready_at": _iso(120),
        "picked_up_at": None,
        "budget": {
            "accept_timeout_s": 180,
            "no_rider_deadline_s": 600,
            "pickup_timeout_s": 300,
            "forward_deadline_s": 300,
        },
    }
    body.update(overrides)
    return body


def _service(routes: dict[str, object], *, with_load: bool = True) -> ExplainService:
    """One MockTransport standing in for order, dispatch and inventory —
    `routes` maps a path fragment to the response each should give."""

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in routes.items():
            if fragment in request.url.path:
                if isinstance(response, Exception):
                    raise response
                return response  # type: ignore[return-value]
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ExplainService(
        timelines=OrderTimelineClient("http://order", http, retry_delay=0.0),
        deliveries=DeliveryClient("http://dispatch", http, retry_delay=0.0),
        kitchen_load=InventoryClient("http://inventory", http, retry_delay=0.0)
        if with_load
        else None,
    )


# ── the happy path ─────────────────────────────────────────────────


async def test_the_three_facts_become_one_sentence():
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body()),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(
                200, json={"active": 2, "capacity": 8, "as_of": NOW.isoformat()}
            ),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.reason is ReasonCode.AWAITING_COURIER
    assert "ready for 2 minutes" in explanation.text
    assert explanation.source == "template"


async def test_an_assigned_courier_changes_the_answer():
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body()),
            "/internal/deliveries": httpx.Response(
                200, json={"state": "ASSIGNED", "assigned_at": _iso(60), "picked_up_at": None}
            ),
            "/load": httpx.Response(404),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.reason is ReasonCode.COURIER_ON_THE_WAY


# ── which missing fact is fatal ────────────────────────────────────


async def test_no_timeline_means_no_explanation():
    """The one fatal absence: a stage nobody observed is not a stage."""
    service = _service({"/timeline": httpx.Response(404)})
    assert await service.explain("ord_ghost", user_id="usr_1") is None


async def test_an_unreachable_order_service_is_not_reported_as_a_missing_order():
    """This used to return None, which the endpoint rendered as
    `404 unknown order` — telling a customer their own order does not
    exist, and registering an outage as a client error that no dashboard
    would alert on."""
    service = _service({"/timeline": httpx.ConnectError("order is down")})
    with pytest.raises(UpstreamUnavailable):
        await service.explain("ord_1", user_id="usr_1")


async def test_an_internal_auth_failure_is_an_outage_not_a_missing_order():
    """A 401/403 on the internal read is our own misconfiguration."""
    service = _service({"/timeline": httpx.Response(403)})
    with pytest.raises(UpstreamUnavailable):
        await service.explain("ord_1", user_id="usr_1")


async def test_an_unreachable_dispatch_costs_only_the_courier_clause():
    """Unlike the timeline, this fact is optional — failing a customer's
    whole explanation over it would be the wrong trade."""
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body()),
            "/internal/deliveries": httpx.Response(503),
            "/load": httpx.Response(404),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.reason is ReasonCode.AWAITING_COURIER


async def test_a_missing_delivery_row_costs_only_the_courier_clause():
    """Ordinary for any order before READY."""
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(status="PREPARING")),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.reason is ReasonCode.KITCHEN_PREPARING


async def test_an_unreachable_inventory_costs_only_the_congestion_clause():
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(status="PREPARING")),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.ConnectError("inventory is down"),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.reason is ReasonCode.KITCHEN_PREPARING
    assert explanation.source == "template"


async def test_a_saturated_kitchen_reaches_the_prose():
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(status="PREPARING")),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(
                200, json={"active": 8, "capacity": 8, "as_of": NOW.isoformat()}
            ),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.reason is ReasonCode.KITCHEN_BUSY
    assert "at capacity" in explanation.text and "8 orders" in explanation.text


async def test_an_assistant_with_no_load_port_still_explains():
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(status="PREPARING")),
            "/internal/deliveries": httpx.Response(404),
        },
        with_load=False,
    )
    assert await service.explain("ord_1", user_id="usr_1") is not None


# ── ownership ──────────────────────────────────────────────────────


async def test_another_customers_order_is_refused():
    service = _service({"/timeline": httpx.Response(200, json=_timeline_body())})
    with pytest.raises(NotYours):
        await service.explain("ord_1", user_id="usr_someone_else")


async def test_ownership_is_checked_before_anyone_else_is_asked():
    """Otherwise anyone holding an order id could make this service
    generate load on dispatch and inventory for orders that are not
    theirs."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if "/timeline" in request.url.path:
            return httpx.Response(200, json=_timeline_body())
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = ExplainService(
        timelines=OrderTimelineClient("http://order", http, retry_delay=0.0),
        deliveries=DeliveryClient("http://dispatch", http, retry_delay=0.0),
        kitchen_load=InventoryClient("http://inventory", http, retry_delay=0.0),
    )
    with pytest.raises(NotYours):
        await service.explain("ord_1", user_id="usr_intruder")
    assert all("/timeline" in path for path in asked), asked


# ── the budget comes from Order, not from us ───────────────────────


async def test_the_deadline_quoted_is_the_one_order_is_running_under():
    """An operator who widens the window changes what "late" means. A
    budget duplicated in this service would go on quoting the old one,
    sounding exactly as confident."""
    tight = _service(
        {
            "/timeline": httpx.Response(
                200,
                json=_timeline_body(budget={"no_rider_deadline_s": 60}),
            ),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    explanation = await tight.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    # 120s ready against a 60s deadline — past it, where the default 600
    # would have said "still looking".
    assert explanation.reason is ReasonCode.AWAITING_COURIER
    assert explanation.bucket.value == "overdue"
    assert "couldn't find a courier in time" in explanation.text


async def test_a_budget_missing_one_knob_keeps_the_others():
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(budget={"pickup_timeout_s": 42})),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.bucket.value == "short"  # default 600s still applies


@pytest.mark.parametrize("budget", ["not-a-dict", None, 7])
async def test_a_shapeless_budget_falls_back_to_the_defaults(budget):
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(budget=budget)),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    assert await service.explain("ord_1", user_id="usr_1") is not None


# ── contract drift ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"user_id": "usr_1", "placed_at": None},  # no birth
        {"user_id": "usr_1", "status": None, "placed_at": _iso(10)},  # no status
        {"user_id": "usr_1", "status": "READY", "placed_at": "not-a-date"},
    ],
)
async def test_a_timeline_we_cannot_read_is_no_explanation(body):
    """First-party drift. It must not raise, and it must not be dressed up
    as a stage — an order with no status is not "preparing"."""
    service = _service({"/timeline": httpx.Response(200, json=body)})
    assert await service.explain("ord_1", user_id="usr_1") is None


async def test_an_unparseable_milestone_is_absent_rather_than_now():
    """Substituting the current time would turn a missing fact into a
    claim that something just happened."""
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body(ready_at="whenever")),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.bucket.value == "unknown"
    assert "minute" not in explanation.text


# ── the endpoint ───────────────────────────────────────────────────


class _FakeExplanations:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[tuple[str, str, str]] = []

    async def drain(self) -> None:
        """Part of the contract: the lifespan drains this on shutdown."""

    async def explain(self, order_id: str, *, user_id: str, locale: str = "en"):
        self.calls.append((order_id, user_id, locale))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _customer():
    from smartfood_auth import AuthContext, headers_for

    return headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))


def test_the_endpoint_returns_the_reason_code_not_just_prose(client):
    """The FE branches on `reason` — a cancelled order offers a reorder, a
    stalled one offers support. A caller forced to pattern-match English
    would break the moment the copy improved."""
    from ai_assistant.domain.render import Bucket, Explanation

    client.app.state.explanations = _FakeExplanations(
        Explanation(
            text="Your food is ready and we're finding a courier to collect it.",
            reason=ReasonCode.AWAITING_COURIER,
            bucket=Bucket.JUST_NOW,
            locale="en",
        )
    )
    r = client.get("/v1/assistant/orders/ord_1/explanation", headers=_customer())
    assert r.status_code == 200
    assert r.json() == {
        "order_id": "ord_1",
        "reason": "awaiting_courier",
        "text": "Your food is ready and we're finding a courier to collect it.",
        "bucket": "just_now",
        "locale": "en",
        "source": "template",
    }


def test_an_unknown_order_and_someone_elses_order_are_the_same_404(client):
    """Confirming an order exists to someone who does not own it is the
    leak, so the two answers must be identical."""
    client.app.state.explanations = _FakeExplanations(None)
    missing = client.get("/v1/assistant/orders/ord_ghost/explanation", headers=_customer())

    client.app.state.explanations = _FakeExplanations(NotYours())
    theirs = client.get("/v1/assistant/orders/ord_1/explanation", headers=_customer())

    assert missing.status_code == theirs.status_code == 404
    assert missing.json()["error"]["code"] == theirs.json()["error"]["code"]
    assert missing.json()["error"]["message"] == theirs.json()["error"]["message"]


def test_the_endpoint_requires_a_signed_in_customer(client):
    client.app.state.explanations = _FakeExplanations(None)
    assert client.get("/v1/assistant/orders/ord_1/explanation").status_code in (401, 403)


def test_the_locale_is_passed_through(client):
    from ai_assistant.domain.render import Bucket, Explanation

    fake = _FakeExplanations(
        Explanation(text="x", reason=ReasonCode.DELIVERED, bucket=Bucket.UNKNOWN, locale="ur")
    )
    client.app.state.explanations = fake
    client.get("/v1/assistant/orders/ord_1/explanation?locale=ur", headers=_customer())
    assert fake.calls == [("ord_1", "usr_1", "ur")]


# ── the rewrite never delays an answer (FR-83) ─────────────────────


async def test_the_answer_does_not_wait_for_a_model():
    """A rewrite changes how an explanation reads and nothing about what it
    says, so a waiting customer must never be charged its latency. The warm
    runs as a task; this proves the explanation is already in hand before
    that task has even started."""
    import asyncio

    from ai_assistant.domain.render import TemplateCache

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowTemplates(TemplateCache):
        polished_calls = 0

        async def warm(self, reason, locale, bucket):
            started.set()
            await release.wait()

        def polished(self, reason, locale, bucket) -> bool:
            SlowTemplates.polished_calls += 1
            return False

    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body()),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    service._templates = SlowTemplates()  # noqa: SLF001

    explanation = await asyncio.wait_for(service.explain("ord_1", user_id="usr_1"), timeout=1.0)
    assert explanation is not None
    assert explanation.source == "template"
    release.set()


async def test_a_plain_template_cache_needs_no_warm_method():
    """The service must work with the bare floor — `warm` is an optional
    capability, not a contract every cache has to implement."""
    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body()),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    assert await service.explain("ord_1", user_id="usr_1") is not None


def test_a_locale_a_customer_invented_collapses_to_a_supported_one():
    """`locale` is a query parameter, so a customer can send anything. It
    is collapsed BEFORE it is used as a template key — otherwise every
    distinct string would be a fresh key for copy that was English either
    way. (It used to be observable through the rewrite cache; that layer is
    gone, so this asserts on the collapse itself.)"""
    from ai_assistant.domain.render import supported

    assert {supported(code) for code in ("en", "zz", "en-GB", "xx-YY", "qqqq")} == {"en"}


async def test_a_broken_template_is_never_rewritten_or_relabelled():
    """`source="fallback"` is the ONLY signal a template is broken — the
    customer sees the same hand-off a genuine UNKNOWN produces. Relabelling
    it "model" would erase the one thing that makes the defect findable."""
    from ai_assistant.domain.render import TemplateCache

    warmed: list[object] = []

    class Broken(TemplateCache):
        def __init__(self) -> None:
            super().__init__({"en": {ReasonCode.AWAITING_COURIER: {None: "{nonexistent}"}}})

        async def warm(self, reason, locale, bucket):
            warmed.append(reason)

        def polished(self, reason, locale, bucket) -> bool:
            return True

    service = _service(
        {
            "/timeline": httpx.Response(200, json=_timeline_body()),
            "/internal/deliveries": httpx.Response(404),
            "/load": httpx.Response(404),
        }
    )
    service._templates = Broken()  # noqa: SLF001
    explanation = await service.explain("ord_1", user_id="usr_1")
    assert explanation is not None
    assert explanation.source == "fallback"
    assert warmed == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, 180),  # bool is a subclass of int — a 1-second window
        (False, 180),
        (-1, 180),
        (0, 180),
        (float("nan"), 180),
        (float("inf"), 180),
        ("600", 180),
        (600, 600),
        (600.0, 600),
    ],
)
def test_a_budget_value_we_cannot_trust_is_absent_not_coerced(value, expected):
    """`True` became a one-second `accept_timeout_s`, and every healthy
    CONFIRMED order was then told the restaurant had missed its window and
    the order would be cancelled. A wrong value, not an absent one."""
    from ai_assistant.adapters.order_client import _budget

    assert _budget({"accept_timeout_s": value}).accept_timeout_s == expected


def test_an_order_service_outage_is_a_503_not_a_404(client):
    """A 404 would tell a customer their own order does not exist, and
    would register the outage as a client error on every dashboard."""

    class Down:
        async def drain(self) -> None: ...

        async def explain(self, order_id, *, user_id, locale="en"):
            raise UpstreamUnavailable("order is down")

    client.app.state.explanations = Down()
    r = client.get("/v1/assistant/orders/ord_1/explanation", headers=_customer())
    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    assert r.json()["error"]["code"] != "NOT_FOUND"
