"""mock-mailer — the fake email provider (S10, FR-41).

A deliberately UNRELIABLE mail service, for the same reason mock-psp is a
deliberately unreliable bank: the interesting part of a sender pipeline is
what happens when sending fails, and a mock that always succeeds would let
the retry/idempotency machinery ship untested. Levers:

- FAIL_RATE (env)              → probabilistic 503s, for chaos runs
- POST /admin/fail_next N      → the next N sends 503, deterministically —
                                 the live lever for watching Celery retry
                                 with backoff and then succeed
- to: *@bounce.invalid         → 400, deterministically — the POISON case:
                                 retrying a rejected recipient can never
                                 help, so the caller must park, not retry

The mock does NOT dedupe. Real providers mostly don't either (an SMTP
retry is a second email) — which is exactly why the caller owns a
delivery_log. A mock that quietly absorbed duplicates would hide the bug
class the log exists to prevent.

In-memory outbox, wiped on restart: GET /mailer/outbox is how demos and
humans verify what "got sent".
"""

import random
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MailerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    fail_rate: float = 0.0  # FAIL_RATE — compose sets it; chaos raises it


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendIn(_In):
    to: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)
    # By-reference on purpose: the attachment is an S3 key, never bytes —
    # the same claim-check rule the task chain itself follows.
    attachment_key: str | None = Field(default=None, max_length=512)


class FailNextIn(_In):
    count: int = Field(ge=0, le=1000)


def create_app(
    settings: MailerSettings | None = None, *, rng: random.Random | None = None
) -> FastAPI:
    settings = settings or MailerSettings()
    rng = rng or random.Random()

    app = FastAPI(title="mock-mailer")
    outbox: list[dict] = []  # newest last — a human-readable sent log
    fail_next = {"count": 0}  # deterministic-failure counter (admin-set)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "mock-mailer"}

    @app.post("/mailer/send", status_code=202)
    async def send(body: SendIn):
        # Rejection FIRST: a bad recipient is bad however the dice land.
        if body.to.endswith("@bounce.invalid"):
            return JSONResponse(status_code=400, content={"error": "recipient rejected"})
        if fail_next["count"] > 0:
            fail_next["count"] -= 1
            return JSONResponse(status_code=503, content={"error": "mailer melting down"})
        if rng.random() < settings.fail_rate:
            return JSONResponse(status_code=503, content={"error": "mailer melting down"})
        message_id = f"msg_{uuid.uuid4().hex[:20]}"
        outbox.append({"message_id": message_id, **body.model_dump()})
        return {"message_id": message_id}

    @app.get("/mailer/outbox")
    async def read_outbox() -> dict:
        return {"emails": outbox}

    @app.post("/admin/fail_next")
    async def set_fail_next(body: FailNextIn) -> dict:
        fail_next["count"] = body.count
        return {"failing_next": fail_next["count"]}

    return app


app = create_app()
