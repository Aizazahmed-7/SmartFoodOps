"""Order and dispatch facts for an explanation (FR-83, FR-86).

Both reads follow `InventoryClient`: system-role identity headers (internal
trust, ADR-0005), two attempts rather than three because this is a render
path and not a money path.

The two differ in one important way, and the difference is the whole
reason they are not one class:

**The timeline is load-bearing.** Without the order's own status there is
nothing to explain, so `OrderTimelineClient.timeline` returns None and the
caller answers with a hand-off rather than inventing a stage.

**The delivery row is optional.** An order that never reached READY has no
delivery, and that 404 is the ordinary case, not a failure. A missing
delivery costs the courier clause and nothing else.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any
from urllib.parse import quote

import httpx
from smartfood_auth import internal_headers
from smartfood_otel import get_logger

from ..domain.explain import Budget, Delivery, Timeline
from ..domain.ports import UpstreamUnavailable
from ..feedback import FeedbackRow


@dataclass(frozen=True)
class OrderFacts:
    """The timeline plus the two ids the caller needs but the resolver does
    not: who owns the order, and which kitchen to ask about. Keeping them
    off `Timeline` keeps that type exactly the resolver's input."""

    timeline: Timeline
    user_id: str
    restaurant_id: str
    budget: Budget = field(default_factory=Budget)
    """The deadlines this order is actually running under, as Order reports
    them — not a copy kept here.

    A duplicated budget drifts silently, and the failure is invisible: an
    operator widens `no_rider_deadline_s` for a holiday and the explanation
    engine goes on quoting the old one, sounding exactly as confident. The
    defaults apply only when Order omits the field, which is contract drift
    rather than configuration.
    """


log = get_logger("ai-assistant.orders")


class _InternalReader:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        attempts: int = 2,
        retry_delay: float = 0.2,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._attempts = attempts
        self._retry_delay = retry_delay

    async def _get(self, path: str) -> dict[str, Any] | None:
        """None = the service answered and there is nothing there.

        Raises `UpstreamUnavailable` when we could not get an answer at
        all. The two were one value, and the effect was that an Order
        outage rendered as `404 unknown order` for a customer looking at
        their own order — a client error the dashboards would never alert
        on, and a lie the page had no way to distinguish from a wrong id.
        """
        url = f"{self._base}{path}"
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.get(url, headers=internal_headers("ai-assistant"))
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                body = resp.json()
                return dict(body) if isinstance(body, dict) else None
            if resp.status_code == 404:
                return None
            if resp.status_code < 500:
                # 401/403 is our own misconfiguration, not a missing order.
                log.warning("internal read refused", path=path, status=resp.status_code)
                raise UpstreamUnavailable(f"{path} refused with {resp.status_code}")
        raise UpstreamUnavailable(f"{path} did not answer")


def _clock(raw: object) -> datetime | None:
    """A moment we cannot parse is a moment we do not have.

    Never "now": these are stamps for stages that already happened, and
    substituting the current time would turn a missing fact into a claim
    that something just occurred.
    """
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class OrderTimelineClient(_InternalReader):
    async def timeline(self, order_id: str) -> OrderFacts | None:
        """None when there is nothing to explain.

        `user_id` comes back so the caller can check the asker owns this
        order. Order deliberately does not enforce that: it holds the
        order's identity and the caller holds the customer's, and the
        comparison belongs where both are known.
        """
        body = await self._get(f"/v1/internal/orders/{quote(order_id, safe='')}/timeline")
        if body is None:
            return None
        placed_at = _clock(body.get("placed_at"))
        if placed_at is None or not isinstance(body.get("status"), str):
            # An order with no status or no birth is not a timeline. This
            # is first-party contract drift, so it must not raise — but it
            # also must not be dressed up as a stage.
            log.warning("order returned a timeline we cannot read", order_id=order_id)
            return None
        return OrderFacts(
            timeline=Timeline(
                status=body["status"],
                placed_at=placed_at,
                confirmed_at=_clock(body.get("confirmed_at")),
                accepted_at=_clock(body.get("accepted_at")),
                preparing_at=_clock(body.get("preparing_at")),
                ready_at=_clock(body.get("ready_at")),
                picked_up_at=_clock(body.get("picked_up_at")),
                cancel_reason=body.get("cancel_reason"),
            ),
            user_id=str(body.get("user_id", "")),
            restaurant_id=str(body.get("restaurant_id", "")),
            budget=_budget(body.get("budget")),
        )


class FeedbackClient(_InternalReader):
    async def for_restaurant(self, claim: str, *, limit: int = 200) -> list[FeedbackRow]:
        """One restaurant's own reviews (FR-92).

        Raises `UpstreamUnavailable` rather than returning an empty list:
        an Order outage must not render as "you have no feedback", which
        an admin would read as a product fact about their own business.
        """
        body = await self._get(
            f"/v1/internal/restaurants/{quote(claim, safe='')}/feedback?limit={int(limit)}"
        )
        if body is None:
            return []
        rows = body.get("feedback")
        if not isinstance(rows, list):
            return []
        return [
            FeedbackRow(
                order_id=str(row.get("order_id", "")),
                rating=int(row.get("rating", 0)),
                comment=row.get("comment"),
                submitted_at=str(row.get("submitted_at", "")),
            )
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("rating"), int)
        ]


class DeliveryClient(_InternalReader):
    async def delivery(self, order_id: str) -> Delivery | None:
        """None when there is no delivery row — which for anything before
        READY is the ordinary case and not an error.

        Also None when dispatch is unreachable. Unlike the timeline, this
        fact is optional: losing it costs the courier clause, and failing a
        customer's whole explanation over it would be the wrong trade.
        """
        try:
            body = await self._get(f"/v1/internal/deliveries/{quote(order_id, safe='')}")
        except UpstreamUnavailable:
            log.warning("dispatch unreachable — explaining without courier facts")
            return None
        if body is None or not isinstance(body.get("state"), str):
            return None
        return Delivery(state=body["state"], assigned_at=_clock(body.get("assigned_at")))


def _budget(raw: object) -> Budget:
    """Order's timer knobs, defaulting field by field.

    Per field rather than all-or-nothing: a payload that gains one knob we
    do not know about should not throw away the three we do.
    """
    if not isinstance(raw, dict):
        return Budget()
    default = Budget()

    def pick(name: str, fallback: int) -> int:
        """A value we cannot trust is an ABSENT value, never a coerced one.

        Three rejections, each seen in practice or one refactor away:

        - `bool` is a subclass of `int`, so `True` silently became a
          one-second `accept_timeout_s` — and every healthy CONFIRMED order
          was then told the restaurant had missed its window and the order
          would be cancelled.
        - `nan`/`inf` raise inside `int()`, and this module is documented as
          never failing an answer. `json.loads` accepts both as bare
          literals, so a first-party service can emit one.
        - A non-positive budget makes every order instantly overdue, which
          reads as a confident prediction of a cancellation that is not
          coming.
        """
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return fallback
        if not isfinite(value) or value <= 0:
            return fallback
        return int(value)

    return Budget(
        accept_timeout_s=pick("accept_timeout_s", default.accept_timeout_s),
        no_rider_deadline_s=pick("no_rider_deadline_s", default.no_rider_deadline_s),
        pickup_timeout_s=pick("pickup_timeout_s", default.pickup_timeout_s),
        forward_deadline_s=pick("forward_deadline_s", default.forward_deadline_s),
    )
