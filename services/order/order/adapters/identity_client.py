"""HTTP adapter for identity's internal address read — same shape as the
catalog client: system identity + audit header + traceparent, retries on
transient failures, 404 is a meaningful permanent answer."""

import asyncio
from typing import Any

import httpx
from smartfood_auth import AuthContext, headers_for
from smartfood_otel import current_traceparent

from ..domain.ports import AddressNotFound, AddressUnavailable

_HEADERS = {
    **headers_for(AuthContext(sub="svc:order", role="system")),
    "X-Internal-Caller": "order",
}


def _headers() -> dict[str, str]:
    headers = dict(_HEADERS)
    if traceparent := current_traceparent():
        headers["traceparent"] = traceparent
    return headers


class IdentityClient:
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

    async def get_address(self, user_id: str, address_id: str) -> dict[str, Any]:
        url = f"{self._base}/v1/internal/users/{user_id}/addresses/{address_id}"
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.get(url, headers=_headers())
            except httpx.HTTPError:
                continue  # network trouble — transient, retry
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise AddressNotFound(address_id)
            if resp.status_code < 500:
                raise AddressUnavailable(f"identity refused address read ({resp.status_code})")
            # 5xx — transient, retry
        raise AddressUnavailable("identity unreachable for address resolution")
