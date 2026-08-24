"""The mock-PSP adapter behind the PaymentGateway port.

The retry rule that makes FR-22 hold: every attempt of one operation
carries the SAME gateway key. A timeout or 5xx retries with that key, and
the PSP's replay memory turns "did it happen?" into a definite answer —
N timeouts can never become two authorizations.
"""

import asyncio
from typing import Any

import httpx

from ..domain.ports import GatewayResult, PspStateConflict, PspUnavailable, PspUnknownRef


class MockPspClient:
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

    async def authorize(
        self, *, key: str, amount_cents: int, currency: str, card_token: str
    ) -> GatewayResult:
        body = {
            "idempotency_key": key,
            "amount_cents": amount_cents,
            "currency": currency,
            "card_token": card_token,
        }
        payload = await self._post("/psp/authorize", body)
        return GatewayResult(approved=payload["outcome"] == "approved", psp_ref=payload["psp_ref"])

    async def capture(self, *, key: str, psp_ref: str) -> None:
        await self._post("/psp/capture", {"idempotency_key": key, "psp_ref": psp_ref})

    async def void(self, *, key: str, psp_ref: str) -> None:
        await self._post("/psp/void", {"idempotency_key": key, "psp_ref": psp_ref})

    async def refund(self, *, key: str, psp_ref: str) -> None:
        await self._post("/psp/refund", {"idempotency_key": key, "psp_ref": psp_ref})

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                resp = await self._http.post(self._base + path, json=body)
            except httpx.HTTPError:
                continue  # network/timeout — retry with the SAME key
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 409:
                raise PspStateConflict(resp.json().get("error", "state conflict"))
            if resp.status_code == 404:
                # The PSP does not know this ref. NOT retryable (it will
                # never learn it) and not a plain outage: the DOMAIN decides
                # per-operation what an unknown auth means (void converges,
                # capture/refund stay loud). Found live: an in-memory
                # mock-psp restart wiped its refs and a compensating void
                # retried 29 times against a hold that no longer existed.
                raise PspUnknownRef(resp.json().get("error", "unknown psp_ref"))
            if resp.status_code < 500:
                # other 4xx: validation — a contract bug, loud once.
                raise PspUnavailable(f"psp refused ({resp.status_code})")
            # 5xx (incl. tok_unknown's ambiguous outcome) — retry, same key.
        raise PspUnavailable("psp unreachable — outcome unknown, key retained")
