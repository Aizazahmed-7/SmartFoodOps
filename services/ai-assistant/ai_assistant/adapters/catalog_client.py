"""Live prices for the dishes an answer named (FR-60).

Mirrors order's `CatalogClient` down to the retry shape: system-role
identity headers (internal trust, ADR-0005), `X-Internal-Caller` for audit,
and the live traceparent so catalog's log lines join this request's trace.

Why the assistant resolves cards rather than the browser: the snapshot
endpoint is SystemOnly and bypasses every cache by design, which is exactly
what FR-60 asks for and exactly what a browser cannot call. It is also what
makes the answer cache safe — a cached answer carries item IDS and no
prices, so a hit renders today's price for a dish, not last hour's
(ADR-0045).
"""

import asyncio
from typing import Any

import httpx
from smartfood_auth import internal_headers
from smartfood_otel import get_logger

log = get_logger("ai-assistant.catalog")


class CatalogClient:
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

    async def snapshot(self, restaurant_id: str, item_ids: list[str]) -> dict[str, Any] | None:
        """None rather than an exception on every failure.

        Order raises here because it is pricing an order and a missing price
        is a refusal. We are decorating an answer that has already been
        written and read: a restaurant that has since been deleted, or a
        catalog that is briefly down, must cost the customer a card — never
        the answer. Two attempts rather than three for the same reason: this
        is on a customer's render path, not a money path.
        """
        url = f"{self._base}/v1/internal/restaurants/{restaurant_id}/snapshot"
        params = httpx.QueryParams([("item_ids", item_id) for item_id in item_ids])
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.get(
                    url, params=params, headers=internal_headers("ai-assistant")
                )
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                return dict(resp.json())
            if resp.status_code < 500:
                # A 404 is a restaurant that has gone since it was indexed —
                # ordinary, not exceptional. Anything else 4xx is a contract
                # bug, and logging it is what makes it findable.
                log.warning(
                    "catalog refused a snapshot — dropping the card",
                    restaurant_id=restaurant_id,
                    status=resp.status_code,
                )
                return None
        log.warning("catalog unreachable — answer renders without cards")
        return None
