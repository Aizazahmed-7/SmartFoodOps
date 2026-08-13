"""HTTP adapter for inventory's reservation API (saga activities).

Business outcomes (item unavailable, at capacity) come back as VALUES —
they steer the workflow's compensation branches. Transport failures raise
InventoryUnavailable, which Temporal's retry policy handles: the
reservation PK (= order_id) makes every retry a safe replay."""

from typing import Any

import httpx
from smartfood_api import ErrorCode
from smartfood_auth import AuthContext, headers_for
from smartfood_otel import current_traceparent

from ..values import LineSpec, ReserveResult

_HEADERS = {
    **headers_for(AuthContext(sub="svc:order-worker", role="system")),
    "X-Internal-Caller": "order-worker",
}


def _headers() -> dict[str, str]:
    headers = dict(_HEADERS)
    if traceparent := current_traceparent():
        headers["traceparent"] = traceparent
    return headers


class InventoryUnavailable(Exception):
    pass


class InventoryClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._http = http

    async def reserve(
        self, *, order_id: str, restaurant_id: str, lines: list[LineSpec]
    ) -> ReserveResult:
        body: dict[str, Any] = {
            "order_id": order_id,
            "restaurant_id": restaurant_id,
            "lines": [{"item_id": line.item_id, "qty": line.qty} for line in lines],
        }
        try:
            resp = await self._http.post(
                f"{self._base}/v1/internal/reservations", json=body, headers=_headers()
            )
        except httpx.HTTPError as exc:
            raise InventoryUnavailable(str(exc)) from None
        if resp.status_code in (200, 201):
            return "ok"
        if resp.status_code == 409:
            code = resp.json()["error"]["code"]
            if code == ErrorCode.ITEM_UNAVAILABLE:
                return "item_unavailable"
            if code == ErrorCode.RESTAURANT_AT_CAPACITY:
                return "at_capacity"
        raise InventoryUnavailable(f"reserve answered {resp.status_code}")

    async def release(self, order_id: str, *, reason: str = "cancelled") -> None:
        try:
            resp = await self._http.post(
                f"{self._base}/v1/internal/reservations/{order_id}/release",
                json={"reason": reason},
                headers=_headers(),
            )
        except httpx.HTTPError as exc:
            raise InventoryUnavailable(str(exc)) from None
        if resp.status_code != 200:
            raise InventoryUnavailable(f"release answered {resp.status_code}")

    async def commit(self, order_id: str) -> None:
        try:
            resp = await self._http.post(
                f"{self._base}/v1/internal/reservations/{order_id}/commit",
                headers=_headers(),
            )
        except httpx.HTTPError as exc:
            raise InventoryUnavailable(str(exc)) from None
        if resp.status_code != 200:
            raise InventoryUnavailable(f"commit answered {resp.status_code}")
