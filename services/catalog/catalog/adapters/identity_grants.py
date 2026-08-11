"""HTTP adapter for the Identity grants port (the ONLY outbound call
Catalog makes — service-ownership.md).

Stamps system-role identity headers (internal-trust model, ADR-0005) plus
X-Internal-Caller for audit. Transient failures (network, 5xx) retry with
linear backoff; any 4xx is permanent and surfaces as GrantRejected.
"""

import asyncio

import httpx
from smartfood_auth import AuthContext, headers_for
from smartfood_otel import current_traceparent

from ..domain.ports import GrantRejected, GrantUnavailable

_HEADERS = {
    **headers_for(AuthContext(sub="svc:catalog", role="system")),
    "X-Internal-Caller": "catalog",
}


def _headers() -> dict[str, str]:
    """Static identity headers + the caller's traceparent (when inside a
    request) so the grant hop logs under the same trace_id at identity."""
    headers = dict(_HEADERS)
    if traceparent := current_traceparent():
        headers["traceparent"] = traceparent
    return headers


class IdentityGrantsClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        attempts: int = 3,
        retry_delay: float = 0.2,
    ):
        self._url = base_url.rstrip("/") + "/v1/internal/grants"
        self._http = http
        self._attempts = attempts
        self._retry_delay = retry_delay

    async def grant_restaurant_admin(self, *, user_id: str, restaurant_id: str) -> None:
        body = {"user_id": user_id, "role": "restaurant_admin", "restaurant_id": restaurant_id}
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.post(self._url, json=body, headers=_headers())
            except httpx.HTTPError:
                continue  # network trouble — transient, retry
            if resp.status_code < 300:
                return
            if resp.status_code < 500:
                raise GrantRejected(f"identity refused grant ({resp.status_code})")
            # 5xx — transient, retry
        raise GrantUnavailable("identity unreachable for onboarding grant")
