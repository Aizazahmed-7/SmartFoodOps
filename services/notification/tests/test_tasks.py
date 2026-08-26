"""The receipts tasks against real sync sqlite + fakes at the ports.

Direct calls execute a task's body synchronously (Celery re-raises into
the caller when there is no worker), so every branch — happy, duplicate,
poison, transient — is a plain function test. The one chain test runs
EAGER (task_always_eager) to prove the render→send handoff: render's
return value arrives as send's first argument through Celery itself.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from botocore.exceptions import ClientError
from notification import tasks
from notification.adapters.contacts import ContactsUnavailable, UnknownRecipient
from notification.adapters.mailer import MailerRejected, MailerUnavailable
from notification.celery_app import celery_app
from notification.db import delivery_log, metadata, receipts
from notification.tasks import (
    Runtime,
    enqueue_receipt_chain,
    render_receipt,
    reset_runtime,
    send_receipt,
    set_runtime,
    sweep_unsent_receipts,
    sync_database_url,
)
from sqlalchemy.pool import StaticPool

SETTLED = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
ITEMS = [{"name": "Chicken Biryani", "qty": 2, "unit_price_cents": 1299, "line_total_cents": 2598}]
TOTALS = {
    "subtotal_cents": 2598,
    "discount_cents": 0,
    "fee_cents": 299,
    "tax_cents": 207,
    "total_cents": 3104,
    "currency": "USD",
}


class FakeStore:
    def __init__(self, boom: Exception | None = None):
        self.puts: list[tuple[str, bytes]] = []
        self._boom = boom

    def put(self, key: str, data: bytes) -> None:
        if self._boom is not None:
            raise self._boom
        self.puts.append((key, data))


class FakeSender:
    def __init__(self, boom: Exception | None = None):
        self.sent: list[dict] = []
        self._boom = boom

    def send(self, *, to: str, subject: str, body: str, attachment_key: str) -> str:
        if self._boom is not None:
            raise self._boom
        self.sent.append(
            {"to": to, "subject": subject, "body": body, "attachment_key": attachment_key}
        )
        return f"msg_{len(self.sent)}"


class FakeContacts:
    def __init__(self, boom: Exception | None = None):
        self.lookups: list[str] = []
        self._boom = boom

    def email_for(self, user_id: str) -> str:
        if self._boom is not None:
            raise self._boom
        self.lookups.append(user_id)
        return f"{user_id}@customers.smartfood.dev"


@pytest.fixture()
def engine():
    engine = sa.create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    metadata.create_all(engine)
    return engine


@pytest.fixture()
def runtime(engine):
    runtime = Runtime(
        engine=engine,
        store=FakeStore(),
        sender=FakeSender(),
        contacts=FakeContacts(),
        sweep_grace=timedelta(seconds=120),
    )
    set_runtime(runtime)
    yield runtime
    reset_runtime()


def _mint(engine, order_id="ord_1", *, created_at=None, failed_at=None):
    with engine.begin() as conn:
        conn.execute(
            receipts.insert().values(
                order_id=order_id,
                user_id="usr_1",
                restaurant_name="Biryani House",
                items=ITEMS,
                totals=TOTALS,
                settled_at=SETTLED,
                created_at=created_at or datetime.now(UTC),
                failed_at=failed_at,
            )
        )


def _receipt_row(engine, order_id="ord_1"):
    with engine.begin() as conn:
        return conn.execute(sa.select(receipts).where(receipts.c.order_id == order_id)).one()


def _delivery_rows(engine):
    with engine.begin() as conn:
        return conn.execute(sa.select(delivery_log)).all()


# ── render ─────────────────────────────────────────────────────────


def test_render_stores_pdf_and_records_the_key(runtime):
    _mint(runtime.engine)
    key = render_receipt("ord_1")
    assert key == "receipts/ord_1.pdf"
    ((stored_key, data),) = runtime.store.puts
    assert stored_key == key and data.startswith(b"%PDF")
    row = _receipt_row(runtime.engine)
    assert row.s3_key == key and row.rendered_at is not None


def test_render_without_a_row_is_a_loud_permanent_failure(runtime):
    with pytest.raises(LookupError):
        render_receipt("ord_ghost")
    assert runtime.store.puts == []


def test_render_storage_blip_is_retryable(runtime, engine):
    """ClientError is in autoretry_for — under a worker this backs off and
    retries; called directly Celery re-raises it to us."""
    _mint(engine)
    set_runtime(
        Runtime(
            engine=engine,
            store=FakeStore(boom=ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")),
            sender=FakeSender(),
            contacts=FakeContacts(),
            sweep_grace=timedelta(seconds=120),
        )
    )
    with pytest.raises(ClientError):
        render_receipt("ord_1")
    assert _receipt_row(engine).s3_key is None  # nothing recorded for a failed put


# ── send ───────────────────────────────────────────────────────────


def test_send_emails_by_reference_and_records_delivery(runtime):
    _mint(runtime.engine)
    message_id = send_receipt("receipts/ord_1.pdf", "ord_1")
    (mail,) = runtime.sender.sent
    assert mail["to"] == "usr_1@customers.smartfood.dev"
    assert mail["subject"] == "Your Biryani House receipt"
    assert mail["attachment_key"] == "receipts/ord_1.pdf"  # a key, never bytes
    (row,) = _delivery_rows(runtime.engine)
    assert (row.order_id, row.channel, row.provider_message_id) == ("ord_1", "email", message_id)


def test_send_is_idempotent_via_the_delivery_log(runtime):
    """The at-least-once absorber: a retry or sweeper re-enqueue finds the
    log row and does NOT email again."""
    _mint(runtime.engine)
    first = send_receipt("receipts/ord_1.pdf", "ord_1")
    again = send_receipt("receipts/ord_1.pdf", "ord_1")
    assert again == first  # the recorded provider id, not a new send
    assert len(runtime.sender.sent) == 1
    assert len(_delivery_rows(runtime.engine)) == 1


def test_send_without_a_row_is_a_loud_permanent_failure(runtime):
    with pytest.raises(LookupError):
        send_receipt("receipts/ord_ghost.pdf", "ord_ghost")


def test_rejected_send_parks_the_receipt_and_never_retries(runtime, engine):
    _mint(engine)
    set_runtime(
        Runtime(
            engine=engine,
            store=FakeStore(),
            sender=FakeSender(boom=MailerRejected("bad recipient")),
            contacts=FakeContacts(),
            sweep_grace=timedelta(seconds=120),
        )
    )
    with pytest.raises(MailerRejected):
        send_receipt("receipts/ord_1.pdf", "ord_1")
    assert _receipt_row(engine).failed_at is not None  # parked, visible
    assert _delivery_rows(engine) == []  # and NOT recorded as sent


def test_unavailable_mailer_leaves_no_trace_and_is_retryable(runtime, engine):
    _mint(engine)
    set_runtime(
        Runtime(
            engine=engine,
            store=FakeStore(),
            sender=FakeSender(boom=MailerUnavailable("503")),
            contacts=FakeContacts(),
            sweep_grace=timedelta(seconds=120),
        )
    )
    with pytest.raises(MailerUnavailable):
        send_receipt("receipts/ord_1.pdf", "ord_1")
    row = _receipt_row(engine)
    assert row.failed_at is None  # transient — NOT parked; retries own it
    assert _delivery_rows(engine) == []


def test_unknown_recipient_parks_the_receipt(runtime, engine):
    """A SETTLED order whose user Identity has never heard of is a data
    bug — retrying cannot conjure the user, so the receipt parks and no
    email attempt is even made."""
    _mint(engine)
    set_runtime(
        Runtime(
            engine=engine,
            store=FakeStore(),
            sender=FakeSender(),
            contacts=FakeContacts(boom=UnknownRecipient("no such user")),
            sweep_grace=timedelta(seconds=120),
        )
    )
    with pytest.raises(UnknownRecipient):
        send_receipt("receipts/ord_1.pdf", "ord_1")
    assert _receipt_row(engine).failed_at is not None  # parked, out of the sweeper
    assert _delivery_rows(engine) == []


def test_identity_outage_is_retryable_and_leaves_no_trace(runtime, engine):
    _mint(engine)
    fake_sender = FakeSender()
    set_runtime(
        Runtime(
            engine=engine,
            store=FakeStore(),
            sender=fake_sender,
            contacts=FakeContacts(boom=ContactsUnavailable("identity 503")),
            sweep_grace=timedelta(seconds=120),
        )
    )
    with pytest.raises(ContactsUnavailable):
        send_receipt("receipts/ord_1.pdf", "ord_1")
    assert _receipt_row(engine).failed_at is None  # transient — retries own it
    assert fake_sender.sent == []  # never reached the provider
    assert _delivery_rows(engine) == []


def test_duplicate_skips_the_identity_lookup(runtime):
    """The delivery-log check comes FIRST: an already-sent receipt must
    not cost an identity round trip (proved by a contacts fake that would
    explode if consulted)."""
    _mint(runtime.engine)
    first = send_receipt("receipts/ord_1.pdf", "ord_1")
    set_runtime(
        Runtime(
            engine=runtime.engine,
            store=FakeStore(),
            sender=FakeSender(),
            contacts=FakeContacts(boom=ContactsUnavailable("must not be called")),
            sweep_grace=timedelta(seconds=120),
        )
    )
    assert send_receipt("receipts/ord_1.pdf", "ord_1") == first  # no raise


# ── the chain, through Celery itself ───────────────────────────────


def test_chain_hands_renders_key_to_send(runtime):
    """EAGER end-to-end: apply_async runs inline, and Celery passes
    render's return value as send's first argument — the by-reference
    handoff the whole design leans on."""
    _mint(runtime.engine)
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        enqueue_receipt_chain("ord_1")
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False
    assert runtime.store.puts[0][0] == "receipts/ord_1.pdf"
    (mail,) = runtime.sender.sent
    assert mail["attachment_key"] == "receipts/ord_1.pdf"
    assert len(_delivery_rows(runtime.engine)) == 1


# ── sweep ──────────────────────────────────────────────────────────


def test_sweep_re_enqueues_exactly_the_owed(runtime, monkeypatch):
    """Four rows, one owed: old+unsent is re-enqueued; fresh (grace),
    parked (failed_at) and already-sent are all left alone."""
    old = datetime.now(UTC) - timedelta(minutes=10)
    _mint(runtime.engine, "ord_owed", created_at=old)
    _mint(runtime.engine, "ord_fresh")  # inside the grace window
    _mint(runtime.engine, "ord_parked", created_at=old, failed_at=datetime.now(UTC))
    _mint(runtime.engine, "ord_sent", created_at=old)
    with runtime.engine.begin() as conn:
        conn.execute(
            delivery_log.insert().values(
                order_id="ord_sent",
                channel="email",
                sent_at=datetime.now(UTC),
                provider_message_id="msg_prior",
            )
        )
    enqueued: list[str] = []
    monkeypatch.setattr(tasks, "enqueue_receipt_chain", enqueued.append)
    assert sweep_unsent_receipts() == 1
    assert enqueued == ["ord_owed"]


def test_sweep_with_nothing_owed_is_quiet(runtime, monkeypatch):
    enqueued: list[str] = []
    monkeypatch.setattr(tasks, "enqueue_receipt_chain", enqueued.append)
    assert sweep_unsent_receipts() == 0
    assert enqueued == []


# ── wiring is contract ─────────────────────────────────────────────


def test_celery_wiring_is_pinned():
    """Names and routes are the wire contract between producer and worker
    (the EventType rule); acks_late is what makes execution at-least-once,
    which is what every idempotency measure above exists to absorb."""
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_ignore_result is True
    assert conf.task_routes["receipts.render"] == {"queue": "receipts.render"}
    assert conf.task_routes["receipts.send"] == {"queue": "receipts.send"}
    assert conf.task_routes["receipts.sweep"] == {"queue": "receipts.send"}
    assert conf.beat_schedule["sweep-unsent-receipts"]["task"] == "receipts.sweep"
    # The registry proves the decorators bound the pinned names (asserted
    # via celery_app.tasks — the decorated objects type as plain functions).
    registered = {name for name in celery_app.tasks if name.startswith("receipts.")}
    assert registered == {"receipts.render", "receipts.send", "receipts.sweep"}


def test_sync_database_url_swaps_async_drivers():
    assert (
        sync_database_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql+psycopg://u:p@h:5432/db"
    )
    assert sync_database_url("sqlite+aiosqlite://") == "sqlite://"
