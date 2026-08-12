"""HTTP surface — system-only internal money operations. Payment is never
edge-routed: humans cannot reach these; saga activities can."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from smartfood_api import ApiError, StrictModel
from smartfood_auth import AuthContext, require_role

from ..domain.ports import PspUnavailable
from ..domain.service import (
    MoneyKeyMismatch,
    MoneyOpInProgress,
    PaymentOutcome,
    PaymentService,
    PaymentStateConflict,
)

router = APIRouter()

SystemOnly = Annotated[AuthContext, Depends(require_role())]  # only role=system


def _svc(request: Request) -> PaymentService:
    return request.app.state.service


class AuthorizeIn(StrictModel):
    amount_cents: int = Field(ge=1, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    card_token: str = Field(pattern=r"^tok_[a-z0-9_]{2,32}$")


def _respond(outcome: PaymentOutcome) -> JSONResponse:
    headers = {"Idempotent-Replay": "true"} if outcome.replayed else None
    return JSONResponse(status_code=outcome.status_code, content=outcome.body, headers=headers)


def _map_money_errors(exc: Exception) -> ApiError:
    if isinstance(exc, MoneyOpInProgress):
        return ApiError(
            "IDEMPOTENCY_IN_PROGRESS",
            "this money operation is already executing",
            409,
            headers={"Retry-After": "1"},
        )
    if isinstance(exc, MoneyKeyMismatch):
        return ApiError("IDEMPOTENCY_KEY_REUSE", "money key reused with different parameters", 422)
    if isinstance(exc, PaymentStateConflict):
        return ApiError("ORDER_STATE_CONFLICT", str(exc), 409)
    assert isinstance(exc, PspUnavailable)
    return ApiError(
        "DEPENDENCY_UNAVAILABLE",
        "payment processor unreachable — retry with the same operation",
        503,
        headers={"Retry-After": "1"},
    )


_MONEY_ERRORS = (MoneyOpInProgress, MoneyKeyMismatch, PaymentStateConflict, PspUnavailable)


@router.post("/v1/internal/payments/{order_id}/authorize")
async def authorize(
    order_id: str, body: AuthorizeIn, ctx: SystemOnly, request: Request
) -> JSONResponse:
    try:
        outcome = await _svc(request).authorize(
            order_id,
            amount_cents=body.amount_cents,
            currency=body.currency,
            card_token=body.card_token,
        )
    except _MONEY_ERRORS as exc:
        raise _map_money_errors(exc) from None
    return _respond(outcome)


@router.post("/v1/internal/payments/{order_id}/capture")
async def capture(order_id: str, ctx: SystemOnly, request: Request) -> JSONResponse:
    try:
        return _respond(await _svc(request).capture(order_id))
    except _MONEY_ERRORS as exc:
        raise _map_money_errors(exc) from None


@router.post("/v1/internal/payments/{order_id}/void")
async def void(order_id: str, ctx: SystemOnly, request: Request) -> JSONResponse:
    try:
        return _respond(await _svc(request).void(order_id))
    except _MONEY_ERRORS as exc:
        raise _map_money_errors(exc) from None


@router.post("/v1/internal/payments/{order_id}/refund")
async def refund(order_id: str, ctx: SystemOnly, request: Request) -> JSONResponse:
    try:
        return _respond(await _svc(request).refund(order_id))
    except _MONEY_ERRORS as exc:
        raise _map_money_errors(exc) from None
