"""HTTP adapter for dispatch's internal cascade API (saga activities).

Pickup coordinates come from CATALOG's public restaurant read, cached
per restaurant for the process lifetime (a restaurant's pin moves ~never;
a stale pin costs meters, and a bounce of the worker clears it). Dropoff
coordinates travel in from the activity (the order row's address
snapshot). Rows seeded before the toy city carry no coordinates — those
fall back to the city center with a warning, so a legacy order limps
instead of wedging the cascade.
"""

from typing import Any

import httpx
from smartfood_auth import internal_headers
from smartfood_otel import get_logger

from ..domain.ports import DispatchUnavailable

log = get_logger("order.dispatch_client")

# Springfield's center — the coordless-legacy-row fallback, matching the
# seed's CITY_BOXES midpoint.
FALLBACK_PICKUP = (39.8000, -89.6500)


def _headers() -> dict[str, str]:
    return internal_headers("order-worker")


class DispatchClient:
    def __init__(self, base_url: str, catalog_base_url: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._catalog = catalog_base_url.rstrip("/")
        self._http = http
        self._pins: dict[str, tuple[float, float]] = {}  # restaurant_id → (lat, lon)

    async def _pickup(self, restaurant_id: str) -> tuple[float, float]:
        cached = self._pins.get(restaurant_id)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(f"{self._catalog}/v1/restaurants/{restaurant_id}")
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise DispatchUnavailable(f"catalog unreachable for pickup pin: {exc!r}") from None
        lat, lon = body.get("lat"), body.get("lon")
        if lat is None or lon is None:
            log.warning("restaurant has no pin — city-center fallback", restaurant=restaurant_id)
            pin = FALLBACK_PICKUP
        else:
            pin = (float(lat), float(lon))
        self._pins[restaurant_id] = pin
        return pin

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(f"{self._base}{path}", json=body, headers=_headers())
        except httpx.HTTPError as exc:
            raise DispatchUnavailable(f"dispatch unreachable: {exc!r}") from None
        if resp.status_code >= 400:
            raise DispatchUnavailable(f"dispatch answered {resp.status_code}")
        return resp.json()

    async def find_and_offer(
        self,
        order_id: str,
        *,
        user_id: str,
        restaurant_id: str,
        restaurant_name: str,
        dropoff: tuple[float, float],
        attempt: int,
        exclude: list[str],
    ) -> dict[str, Any]:
        pickup = await self._pickup(restaurant_id)
        return await self._post(
            "/v1/internal/dispatch/offers",
            {
                "order_id": order_id,
                "user_id": user_id,
                "restaurant_name": restaurant_name,
                "pickup": {"lat": pickup[0], "lon": pickup[1]},
                "dropoff": {"lat": dropoff[0], "lon": dropoff[1]},
                "attempt": attempt,
                "exclude": exclude,
            },
        )

    async def expire_offer(self, order_id: str, *, offer_id: str, rider_id: str) -> dict[str, Any]:
        return await self._post(
            "/v1/internal/dispatch/offers/expire",
            {"order_id": order_id, "offer_id": offer_id, "rider_id": rider_id},
        )

    async def unassign_stalled(self, order_id: str, *, rider_id: str) -> dict[str, Any]:
        return await self._post(
            f"/v1/internal/dispatch/orders/{order_id}/unassign", {"rider_id": rider_id}
        )

    async def cancel(self, order_id: str) -> dict[str, Any]:
        return await self._post(f"/v1/internal/dispatch/orders/{order_id}/cancel", {})
