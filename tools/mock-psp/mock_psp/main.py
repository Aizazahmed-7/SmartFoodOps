"""mock-psp — the fake payment processor (ADR-0010).

A deliberately UNRELIABLE bank, because the whole point is exercising the
recovery machinery: probabilistic failure knobs (env) plus magic card
tokens that force a specific failure deterministically, regardless of the
knobs (docs/local-dev.md §9):

- tok_decline  → authorization declined (a business outcome, not an error)
- tok_timeout  → records the authorization, then hangs — the caller times
                 out; the retry with the same idempotency key replays the
                 recorded approval (the "N timeouts, ≤1 auth" invariant)
- tok_unknown  → records the authorization, then answers 500 — the
                 ambiguous-outcome case; same-key retry resolves it

Everything is keyed by the caller's idempotency_key and REPLAYED on
repetition — real PSPs work exactly this way, and payment's correctness
leans on it. In-memory state: this is a dev tool, wiped on restart.
"""

import asyncio
import random
import uuid
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PspSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    decline_rate: float = 0.0  # DECLINE_RATE etc. — compose sets these
    timeout_rate: float = 0.0
    unknown_outcome_rate: float = 0.0
    latency_ms_p50: int = 0
    timeout_sleep_seconds: float = 30.0  # tok_timeout hang; tiny in tests


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizeIn(_In):
    idempotency_key: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(ge=1, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    card_token: str = Field(min_length=1, max_length=64)


class RefIn(_In):
    idempotency_key: str = Field(min_length=1, max_length=64)
    psp_ref: str = Field(min_length=1, max_length=64)


def create_app(settings: PspSettings | None = None, *, rng: random.Random | None = None) -> FastAPI:
    settings = settings or PspSettings()
    rng = rng or random.Random()

    app = FastAPI(title="mock-psp")
    # answered[key] = (status_code, body) — the PSP's replay memory.
    answered: dict[str, tuple[int, dict]] = {}
    # refs[psp_ref] = lifecycle state of the authorization.
    refs: dict[str, str] = {}

    async def _latency() -> None:
        if settings.latency_ms_p50:
            await asyncio.sleep(settings.latency_ms_p50 / 1000)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mock-psp"}

    @app.post("/psp/authorize")
    async def authorize(body: AuthorizeIn):
        await _latency()
        if body.idempotency_key in answered:  # replay FIRST — before any dice
            status, stored = answered[body.idempotency_key]
            return JSONResponse(status_code=status, content=stored)

        def record(outcome: Literal["approved", "declined"]) -> dict:
            psp_ref = f"psp_{uuid.uuid4().hex[:20]}"
            if outcome == "approved":
                refs[psp_ref] = "authorized"
            payload = {"psp_ref": psp_ref, "outcome": outcome}
            answered[body.idempotency_key] = (200, payload)
            return payload

        # Magic tokens override the dice — deterministic failures for tests.
        if body.card_token == "tok_decline":
            return record("declined")
        if body.card_token == "tok_timeout":
            payload = record("approved")  # recorded BEFORE the hang
            await asyncio.sleep(settings.timeout_sleep_seconds)
            return payload
        if body.card_token == "tok_unknown":
            record("approved")  # recorded, but the caller sees a 500
            return JSONResponse(status_code=500, content={"error": "internal"})

        roll = rng.random()
        if roll < settings.decline_rate:
            return record("declined")
        if roll < settings.decline_rate + settings.timeout_rate:
            payload = record("approved")
            await asyncio.sleep(settings.timeout_sleep_seconds)
            return payload
        if roll < settings.decline_rate + settings.timeout_rate + settings.unknown_outcome_rate:
            record("approved")
            return JSONResponse(status_code=500, content={"error": "internal"})
        return record("approved")

    def _lifecycle(body: RefIn, *, expected: str, target: str, op: str):
        """capture/void/refund share one shape: replay by key, 404 unknown
        ref, guarded state transition (wrong state on a FRESH key → 409)."""
        if body.idempotency_key in answered:
            status, stored = answered[body.idempotency_key]
            return JSONResponse(status_code=status, content=stored)
        state = refs.get(body.psp_ref)
        if state is None:
            return JSONResponse(status_code=404, content={"error": "unknown psp_ref"})
        if state != expected:
            return JSONResponse(
                status_code=409, content={"error": f"cannot {op} a {state} authorization"}
            )
        refs[body.psp_ref] = target
        payload = {"psp_ref": body.psp_ref, "outcome": target}
        answered[body.idempotency_key] = (200, payload)
        return JSONResponse(status_code=200, content=payload)

    @app.post("/psp/capture")
    async def capture(body: RefIn):
        await _latency()
        return _lifecycle(body, expected="authorized", target="captured", op="capture")

    @app.post("/psp/void")
    async def void(body: RefIn):
        await _latency()
        return _lifecycle(body, expected="authorized", target="voided", op="void")

    @app.post("/psp/refund")
    async def refund(body: RefIn):
        await _latency()
        return _lifecycle(body, expected="captured", target="refunded", op="refund")

    return app


app = create_app()
