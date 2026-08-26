"""The sender port (FR-41) and its dev implementation.

`Sender` is the seam the tasks depend on: swap HttpMailer for an SES
client (or an SQS enqueue toward a sending Lambda) without touching a
task. The port's error contract is the part that matters, because Celery's
retry policy keys off it:

- MailerUnavailable → RETRYABLE. The provider was unreachable or melting
  down (5xx, timeout, connection refused); trying again later is exactly
  right, and autoretry+backoff handles it.
- MailerRejected → POISON. The provider looked at the request and said no
  (4xx — bad recipient, oversized body). Retrying an unchanged request can
  never change the answer, so the task parks the receipt (failed_at)
  instead of burning retries — the DLQ philosophy, applied to email.

Sync on purpose: Celery tasks are sync, so the client is sync httpx.
"""

from typing import Protocol

import httpx


class MailerUnavailable(Exception):
    """Transient provider failure — safe and correct to retry."""


class MailerRejected(Exception):
    """The provider refused the message — retrying cannot help."""


class Sender(Protocol):
    def send(self, *, to: str, subject: str, body: str, attachment_key: str) -> str: ...


class HttpMailer:
    """mock-mailer over HTTP. One client per instance; instances are built
    per prefork child (see tasks.py), never before the fork."""

    def __init__(
        self, base_url: str, *, timeout_s: float = 5.0, transport: httpx.BaseTransport | None = None
    ):
        # transport is the test seam (httpx.MockTransport).
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def send(self, *, to: str, subject: str, body: str, attachment_key: str) -> str:
        try:
            resp = self._client.post(
                "/mailer/send",
                json={"to": to, "subject": subject, "body": body, "attachment_key": attachment_key},
            )
        except httpx.HTTPError as exc:
            raise MailerUnavailable(f"mailer unreachable: {exc!r}") from None
        if resp.status_code >= 500:
            raise MailerUnavailable(f"mailer 5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise MailerRejected(f"mailer refused ({resp.status_code}): {resp.text}")
        return str(resp.json()["message_id"])
