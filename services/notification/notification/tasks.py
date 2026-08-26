"""The receipts pipeline's worker half — render, send, sweep.

Everything here is SYNC on purpose: Celery's execution model is a sync
function per prefork child, so the tasks speak sync SQLAlchemy (psycopg),
sync httpx and boto3 — bringing the service's async stack in would mean
running a private event loop per task for zero concurrency gain.

The Runtime is built LAZILY, on the first task a worker runs, under a
lock — never at import. Two reasons, one per pool type: under prefork,
an engine or HTTP client created at import is duplicated across forked
children sharing file descriptors (the classic prefork corruption);
under the threads pool (what dev compose runs — see celery_app.py for
why), two tasks can race the first build. `set_runtime` is the test seam
that injects sqlite + fakes in place of the live wiring; everything in
the Runtime (engine pool, httpx.Client, boto3 client) is thread-safe.

Idempotency map (execution is at-least-once by config — acks_late):
- render: deterministic key ⇒ a replay overwrites the same object with
  the same bytes. Naturally idempotent, no bookkeeping needed.
- send:   delivery_log existence-check before, record after. The residual
  window (crash between provider-accept and record) can duplicate ONE
  email — chosen over the claim-first alternative, which converts the
  same crash into a receipt that never arrives (see the walkthrough's
  pros/cons; for money documents, duplicate beats missing).
- sweep:  re-enqueues are absorbed by the two properties above, so the
  sweeper can afford to be dumb.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from botocore.exceptions import BotoCoreError, ClientError
from celery import chain
from prometheus_client import Counter
from smartfood_otel import REGISTRY, get_logger

from .adapters.contacts import Contacts, ContactsUnavailable, HttpContacts, UnknownRecipient
from .adapters.mailer import HttpMailer, MailerRejected, MailerUnavailable, Sender
from .adapters.repo import insert_ignoring_conflict
from .adapters.storage import S3ReceiptStore, receipt_key
from .celery_app import celery_app
from .config import Settings
from .db import delivery_log, receipts
from .domain.receipt import ReceiptData, receipt_body, receipt_pdf, receipt_subject

log = get_logger("notification.tasks")

RECEIPTS_RENDERED = Counter(
    "receipts_rendered_total",
    "Receipt PDFs rendered and stored.",
    registry=REGISTRY,
)
RECEIPTS_SENT = Counter(
    "receipts_sent_total",
    "Receipt send attempts by outcome.",
    labelnames=("outcome",),  # sent | duplicate | rejected
    registry=REGISTRY,
)
RECEIPTS_SWEPT = Counter(
    "receipts_swept_total",
    "Owed-but-unsent receipts the beat sweeper re-enqueued.",
    registry=REGISTRY,
)


class ReceiptStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...


@dataclass
class Runtime:
    """One per worker CHILD process (and one per test, injected)."""

    engine: sa.Engine
    store: ReceiptStore
    sender: Sender
    contacts: Contacts
    sweep_grace: timedelta


_runtime: Runtime | None = None
_runtime_lock = threading.Lock()


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime


def reset_runtime() -> None:
    set_runtime(None)


def sync_database_url(url: str) -> str:
    """The tasks speak sync drivers: asyncpg → psycopg, aiosqlite → stdlib."""
    return url.replace("+asyncpg", "+psycopg").replace("+aiosqlite", "")


def _get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:  # pragma: no cover — live wiring (built per worker, post-spawn)
        with _runtime_lock:
            if _runtime is None:  # two threads can race the first task
                settings = Settings()
                _runtime = Runtime(
                    engine=sa.create_engine(
                        sync_database_url(settings.database_url), pool_size=2, pool_pre_ping=True
                    ),
                    store=S3ReceiptStore(
                        settings.receipts_bucket, endpoint_url=settings.aws_endpoint_url
                    ),
                    sender=HttpMailer(
                        settings.mailer_base_url, timeout_s=settings.mailer_timeout_seconds
                    ),
                    contacts=HttpContacts(
                        settings.identity_base_url, timeout_s=settings.contacts_timeout_seconds
                    ),
                    sweep_grace=timedelta(seconds=settings.receipt_sweep_grace_seconds),
                )
    return _runtime


def enqueue_receipt_chain(order_id: str) -> None:
    """render → send, one message pair. The chain hands render's RETURN
    VALUE (the s3 key) to send as its first argument — a reference rides
    the broker, the bytes never do. Signatures are built by WIRE NAME (the
    same names task_routes and the workers key on), not by attribute —
    the producer speaks the contract, not the import graph."""
    chain(
        celery_app.signature("receipts.render", args=(order_id,)),
        celery_app.signature("receipts.send", args=(order_id,)),
    ).apply_async()


def _aware(value: Any) -> datetime:
    """sqlite hands back naive timestamps; Postgres hands back aware ones.
    Either way the document prints the UTC instant the event carried."""
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _receipt_data(row: sa.Row[Any]) -> ReceiptData:
    return ReceiptData(
        order_id=row.order_id,
        user_id=row.user_id,
        restaurant_name=row.restaurant_name,
        items=row.items,
        totals=row.totals,
        settled_at=_aware(row.settled_at),
    )


def _park(runtime: Runtime, order_id: str) -> None:
    """Poison handling, shared by every permanent send failure: a non-null
    failed_at pulls the row out of the sweeper and every retry loop —
    clearing it after a fix is the human replay lever (see runbooks)."""
    with runtime.engine.begin() as conn:
        conn.execute(
            receipts.update()
            .where(receipts.c.order_id == order_id)
            .values(failed_at=datetime.now(UTC))
        )


def _load_receipt(conn: sa.Connection, order_id: str) -> sa.Row[Any]:
    row = conn.execute(sa.select(receipts).where(receipts.c.order_id == order_id)).one_or_none()
    if row is None:
        # Permanent by construction: enqueues happen only after the row's
        # transaction commits, and rows are never deleted. Not retryable —
        # LookupError is outside every autoretry_for below, so the task
        # fails LOUDLY instead of burning a backoff schedule on a bug.
        raise LookupError(f"no receipt row for {order_id}")
    return row


@celery_app.task(
    name="receipts.render",
    autoretry_for=(BotoCoreError, ClientError),  # S3 blips — the storage half
    retry_backoff=2,  # deterministic 2/4/8…s, capped below
    retry_backoff_max=300,
    # Full jitter (celery's retry_jitter) draws uniformly from [0, delay] —
    # watched live rolling 0s,0s,0s and hammering the dead provider. Herd
    # protection matters at fleet scale; at single-digit workers, visible
    # deterministic spacing wins. Revisit when senders scale out.
    retry_jitter=False,
    max_retries=8,
)
def render_receipt(order_id: str) -> str:
    """CPU half: claim-check row → PDF bytes → S3, return the key."""
    runtime = _get_runtime()
    with runtime.engine.begin() as conn:
        row = _load_receipt(conn, order_id)
    key = receipt_key(order_id)
    runtime.store.put(key, receipt_pdf(_receipt_data(row)))
    with runtime.engine.begin() as conn:
        conn.execute(
            receipts.update()
            .where(receipts.c.order_id == order_id)
            .values(s3_key=key, rendered_at=datetime.now(UTC))
        )
    RECEIPTS_RENDERED.inc()
    log.info("receipt rendered", order_id=order_id, s3_key=key)
    return key


@celery_app.task(
    name="receipts.send",
    # Transient pain from EITHER dependency retries: the provider melting
    # (MailerUnavailable) or identity unreachable (ContactsUnavailable).
    autoretry_for=(MailerUnavailable, ContactsUnavailable),
    retry_backoff=2,
    retry_backoff_max=300,
    retry_jitter=False,  # same reasoning as render — see above
    max_retries=8,
)
def send_receipt(s3_key: str, order_id: str) -> str:
    """I/O half: check the log, resolve the recipient, send by reference,
    record the send."""
    runtime = _get_runtime()
    with runtime.engine.begin() as conn:
        sent = conn.execute(
            sa.select(delivery_log).where(
                (delivery_log.c.order_id == order_id) & (delivery_log.c.channel == "email")
            )
        ).one_or_none()
        if sent is not None:
            # A sweeper re-enqueue, or a retry that lost the crash race —
            # either way the email exists and this delivery is a no-op.
            RECEIPTS_SENT.labels(outcome="duplicate").inc()
            log.info("receipt already sent — skipping", order_id=order_id)
            return str(sent.provider_message_id)
        row = _load_receipt(conn, order_id)

    data = _receipt_data(row)
    # Resolve AFTER the duplicate check (no identity call for a no-op) and
    # at SEND time, not consume time: the customer gets the receipt at the
    # address they use NOW. The address itself never touches the logs.
    try:
        to = runtime.contacts.email_for(data.user_id)
    except UnknownRecipient:
        _park(runtime, order_id)
        RECEIPTS_SENT.labels(outcome="no_recipient").inc()
        log.error(
            "identity has no user for this receipt — parked",
            order_id=order_id,
            user_id=data.user_id,
        )
        raise

    try:
        message_id = runtime.sender.send(
            to=to,
            subject=receipt_subject(data),
            body=receipt_body(data),
            attachment_key=s3_key,
        )
    except MailerRejected:
        # Poison: the provider REFUSED (4xx). Park and fail without
        # retries (MailerRejected is outside autoretry_for).
        _park(runtime, order_id)
        RECEIPTS_SENT.labels(outcome="rejected").inc()
        log.error("mailer rejected the receipt — parked", order_id=order_id)
        raise

    with runtime.engine.begin() as conn:
        conn.execute(
            insert_ignoring_conflict(
                delivery_log,
                {
                    "order_id": order_id,
                    "channel": "email",
                    "sent_at": datetime.now(UTC),
                    "provider_message_id": message_id,
                },
                ["order_id", "channel"],
                conn.dialect.name,
            )
        )
    RECEIPTS_SENT.labels(outcome="sent").inc()
    log.info("receipt sent", order_id=order_id, provider_message_id=message_id)
    return message_id


@celery_app.task(name="receipts.sweep")
def sweep_unsent_receipts() -> int:
    """The reconciler (beat, every receipt_sweep_seconds): any receipt old
    enough to be past the grace window, not parked, and absent from the
    delivery log gets its chain re-enqueued. This is what lets the
    post-commit enqueue in the consumer be best-effort — a lost nudge
    costs one sweep interval, never the receipt."""
    runtime = _get_runtime()
    cutoff = datetime.now(UTC) - runtime.sweep_grace
    with runtime.engine.begin() as conn:
        owed = (
            conn.execute(
                sa.select(receipts.c.order_id)
                .select_from(
                    receipts.outerjoin(
                        delivery_log,
                        (delivery_log.c.order_id == receipts.c.order_id)
                        & (delivery_log.c.channel == "email"),
                    )
                )
                .where(
                    delivery_log.c.order_id.is_(None)
                    & receipts.c.failed_at.is_(None)
                    & (receipts.c.created_at < cutoff)
                )
                .limit(100)  # a stampede of debt drains over several sweeps
            )
            .scalars()
            .all()
        )
    for order_id in owed:
        enqueue_receipt_chain(order_id)
    if owed:
        RECEIPTS_SWEPT.inc(len(owed))
        # warning, not info: a non-empty sweep means enqueues are being
        # lost or sends are stalling — worth a human glance either way.
        log.warning("sweeper re-enqueued unsent receipts", count=len(owed))
    return len(owed)
