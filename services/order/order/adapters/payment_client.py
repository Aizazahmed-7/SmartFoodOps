"""HTTP adapter for payment's money operations (saga activities).

Declines are VALUES (a business branch); transport trouble raises
PaymentUnavailable for Temporal to retry — safe because payment's money
keys ({order_id}:{op}) make every retry converge on one truth (FR-22)."""

import httpx
from smartfood_api import ErrorCode
from smartfood_auth import internal_headers

from ..domain.ports import PaymentStateConflict
from ..values import AuthResult


def _headers() -> dict[str, str]:
    return internal_headers("order-worker")


class PaymentUnavailable(Exception):
    pass


class PaymentClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._http = http

    async def authorize(
        self, order_id: str, *, amount_cents: int, currency: str, card_token: str
    ) -> AuthResult:
        try:
            resp = await self._http.post(
                f"{self._base}/v1/internal/payments/{order_id}/authorize",
                json={
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "card_token": card_token,
                },
                headers=_headers(),
            )
        except httpx.HTTPError as exc:
            raise PaymentUnavailable(str(exc)) from None
        if resp.status_code == 200:
            return "ok"
        if resp.status_code == 402:
            return "declined"
        raise PaymentUnavailable(f"authorize answered {resp.status_code}")

    async def void(self, order_id: str) -> None:
        await self._lifecycle(order_id, "void", converge_on_conflict=True)

    async def capture(self, order_id: str) -> None:
        # NOT convergent: capture-with-no-auth means settling without money.
        await self._lifecycle(order_id, "capture", converge_on_conflict=False)

    async def refund(self, order_id: str) -> None:
        await self._lifecycle(order_id, "refund", converge_on_conflict=True)

    async def _lifecycle(self, order_id: str, op: str, *, converge_on_conflict: bool) -> None:
        try:
            resp = await self._http.post(
                f"{self._base}/v1/internal/payments/{order_id}/{op}", headers=_headers()
            )
        except httpx.HTTPError as exc:
            raise PaymentUnavailable(str(exc)) from None
        if resp.status_code == 200:
            return
        if resp.status_code == 409:
            code = resp.json()["error"]["code"]
            if code == ErrorCode.ORDER_STATE_CONFLICT:
                if converge_on_conflict:
                    # Compensation convergence: voiding/refunding when no auth
                    # or capture exists is success, not failure — a
                    # compensation must never wedge on "nothing to undo".
                    return
                # Forward-path capture found nothing to take: retrying can
                # never help, and settling anyway would ship food for free.
                raise PaymentStateConflict(f"{op} on {order_id}: no capturable authorization")
        raise PaymentUnavailable(f"{op} answered {resp.status_code}")
