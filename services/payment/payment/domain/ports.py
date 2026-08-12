"""The PaymentGateway port (ADR-0010) — the ONLY seam through which any
PSP is reached. Swapping mock→Stripe is a new adapter; nothing else moves."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GatewayResult:
    approved: bool
    psp_ref: str


class PspUnavailable(Exception):
    """PSP unreachable/ambiguous after retries. The SAME gateway key must be
    used on the next attempt — the PSP's replay is what resolves ambiguity."""


class PspStateConflict(Exception):
    """The PSP refused a lifecycle op (capture a voided auth, etc.)."""


class PaymentGatewayPort(Protocol):
    async def authorize(
        self, *, key: str, amount_cents: int, currency: str, card_token: str
    ) -> GatewayResult:
        """Declines are RESULTS (approved=False), not exceptions — a decline
        is a business outcome; PspUnavailable is a transport failure."""
        ...

    async def capture(self, *, key: str, psp_ref: str) -> None: ...

    async def void(self, *, key: str, psp_ref: str) -> None: ...

    async def refund(self, *, key: str, psp_ref: str) -> None: ...
