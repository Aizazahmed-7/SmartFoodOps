"""HTTP adapter for the Catalog snapshot port.

Mirrors catalog's identity_grants client: system-role identity headers
(internal-trust, ADR-0005) + X-Internal-Caller for audit + traceparent so
catalog's log lines join this request's trace. Transient failures (network,
5xx) retry with linear backoff; 404 is permanent (unknown restaurant)."""

import asyncio
from typing import Any

import httpx
from smartfood_auth import internal_headers

from ..domain.ports import RestaurantNotFound, SnapshotUnavailable


def _headers() -> dict[str, str]:
    return internal_headers("order")


class CatalogClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        attempts: int = 3,
        retry_delay: float = 0.2,
    ):
        self._base = base_url.rstrip("/")
        self._http = http
        self._attempts = attempts
        self._retry_delay = retry_delay

    async def get_snapshot(self, restaurant_id: str, item_ids: list[str]) -> dict[str, Any]:
        url = f"{self._base}/v1/internal/restaurants/{restaurant_id}/snapshot"
        params = httpx.QueryParams([("item_ids", item_id) for item_id in item_ids])
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.get(url, params=params, headers=_headers())
            except httpx.HTTPError:
                continue  # network trouble — transient, retry
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise RestaurantNotFound(restaurant_id)
            if resp.status_code < 500:
                # Any other 4xx is a contract bug between us and catalog —
                # loud beats silent, and retrying can't fix a refusal.
                raise SnapshotUnavailable(f"catalog refused snapshot ({resp.status_code})")
            # 5xx — transient, retry
        raise SnapshotUnavailable("catalog unreachable for pricing snapshot")
