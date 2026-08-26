"""Recipient resolution — user_id → the address a receipt goes to.

Order events deliberately carry no PII (events are forever, replayed, and
readable by every consumer — ADR-0002's posture), so the recipient's
email is resolved at SEND time against Identity, the service that owns
it. Send-time resolution is a feature, not a compromise: a customer who
changed their email after ordering gets the receipt where they read mail
NOW. The alternatives — copying the email into the claim check at consume
time, or projecting an identity event stream locally — are argued in the
walkthrough's pros/cons ledger; both trade freshness or add PII copies
for a dependency this task queue absorbs for free (retries with backoff
are the native failure mode here).

The error contract mirrors the mailer's, because Celery's retry policy
keys off it:

- ContactsUnavailable → transient (identity unreachable / 5xx). Retrying
  is right, and autoretry+backoff owns it.
- UnknownRecipient → permanent (404). A SETTLED order pointing at a user
  Identity has never heard of is a data bug — retrying cannot conjure the
  user, so the receipt parks, loudly.
"""

from typing import Protocol

import httpx
from smartfood_auth import internal_headers


class ContactsUnavailable(Exception):
    """Identity was unreachable or failing — safe and correct to retry."""


class UnknownRecipient(Exception):
    """Identity has no such user — retrying cannot help."""


class Contacts(Protocol):
    def email_for(self, user_id: str) -> str: ...


class HttpContacts:
    """Identity's internal contact read, over sync httpx (the Celery
    world). One client per instance; instances are built per worker by
    the lazy Runtime, never at import."""

    def __init__(
        self, base_url: str, *, timeout_s: float = 5.0, transport: httpx.BaseTransport | None = None
    ):
        # transport is the test seam (httpx.MockTransport).
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def email_for(self, user_id: str) -> str:
        try:
            resp = self._client.get(
                f"/v1/internal/users/{user_id}",
                headers=internal_headers("notification-worker"),
            )
        except httpx.HTTPError as exc:
            raise ContactsUnavailable(f"identity unreachable: {exc!r}") from None
        if resp.status_code == 404:
            raise UnknownRecipient(f"identity has no user {user_id}")
        if resp.status_code >= 400:
            # 5xx AND unexpected 4xx (403 = misconfigured system headers)
            # are both "not the recipient's fault" — retryable, and loud in
            # the worker log either way.
            raise ContactsUnavailable(f"identity answered {resp.status_code}")
        return str(resp.json()["email"])
