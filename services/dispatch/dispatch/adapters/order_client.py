"""Courier events → the order service, which signals its own workflows.

Dispatch never touches Temporal: the kitchen precedent holds — a service
signals only the workflows it owns, so accept/pickup/deliver facts travel
as internal HTTP to order, and order raises the signal on dlv::{order_id}.

Outcome vocabulary (values, not exceptions — the saga clients' idiom):
  ok    — the signal was raised
  gone  — order answered 404 (no such workflow — already terminal). NOT an
          error here: DDB is dispatch's truth, and the workflow's own
          revoke/read path reconciles. Logged, surfaced, never raised.
Transient failures (5xx/network) retry ×3 then raise OrderUnavailable —
the caller's HTTP surface turns that into a 503 the rider app retries.
"""

import asyncio
from typing import Literal

import httpx
from smartfood_auth import internal_headers
from smartfood_otel import get_logger

from ..domain.service import CourierEvent, OrderUnavailable

log = get_logger("dispatch.order_client")

Outcome = Literal["ok", "gone"]


class OrderCourierClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient, *, attempts: int = 3):
        self._base = base_url.rstrip("/")
        self._http = http
        self._attempts = attempts

    async def send(self, order_id: str, *, event: CourierEvent, rider_id: str) -> Outcome:
        url = f"{self._base}/v1/internal/orders/{order_id}/courier"
        body = {"event": event, "rider_id": rider_id}
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(0.2 * attempt)
            try:
                resp = await self._http.post(url, json=body, headers=internal_headers("dispatch"))
            except httpx.HTTPError:
                continue  # transient — retry
            if resp.status_code < 300:
                return "ok"
            if resp.status_code == 404:
                # kwarg is courier_event: `event` is structlog's message slot
                log.warning(
                    "courier event for a gone workflow", order_id=order_id, courier_event=event
                )
                return "gone"
            if resp.status_code < 500:
                # A 4xx here is OUR contract bug — loud beats swallowed.
                raise OrderUnavailable(f"order refused courier event ({resp.status_code})")
        raise OrderUnavailable("order unreachable for courier event")
