"""The receipt adapters at their seams: a fake boto client for the store,
httpx.MockTransport for the mailer — the error CONTRACT (retryable vs
poison) is the thing under test, because Celery's policy keys off it."""

import httpx
import pytest
from botocore.exceptions import ClientError
from notification.adapters.contacts import ContactsUnavailable, HttpContacts, UnknownRecipient
from notification.adapters.mailer import HttpMailer, MailerRejected, MailerUnavailable
from notification.adapters.storage import S3ReceiptStore, receipt_key


class FakeS3Client:
    def __init__(self, *, missing_bucket_puts: int = 0, boom: Exception | None = None):
        self.puts: list[dict] = []
        self.created: list[str] = []
        self._missing = missing_bucket_puts
        self._boom = boom

    def put_object(self, **kwargs):
        if self._boom is not None:
            raise self._boom
        if self._missing > 0:
            self._missing -= 1
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")
        self.puts.append(kwargs)

    def create_bucket(self, *, Bucket: str):  # noqa: N803 — boto3's casing
        self.created.append(Bucket)


def test_receipt_key_is_deterministic():
    assert receipt_key("ord_1") == "receipts/ord_1.pdf"


def test_put_stores_pdf_bytes():
    client = FakeS3Client()
    S3ReceiptStore("sfo-receipts", client=client).put("receipts/ord_1.pdf", b"%PDF")
    (put,) = client.puts
    assert put["Bucket"] == "sfo-receipts" and put["Key"] == "receipts/ord_1.pdf"
    assert put["ContentType"] == "application/pdf"
    assert client.created == []


def test_missing_bucket_self_heals():
    """Fresh LocalStack volume: the first put creates the bucket and
    retries — no seeding step, no boot-order race."""
    client = FakeS3Client(missing_bucket_puts=1)
    S3ReceiptStore("sfo-receipts", client=client).put("receipts/ord_1.pdf", b"%PDF")
    assert client.created == ["sfo-receipts"]
    assert len(client.puts) == 1


def test_other_s3_errors_stay_loud():
    client = FakeS3Client(boom=ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"))
    with pytest.raises(ClientError):
        S3ReceiptStore("sfo-receipts", client=client).put("receipts/ord_1.pdf", b"%PDF")
    assert client.created == []  # only NoSuchBucket triggers the heal


def _mailer(handler) -> HttpMailer:
    return HttpMailer("http://mailer.test", transport=httpx.MockTransport(handler))


def _send(mailer: HttpMailer) -> str:
    return mailer.send(
        to="usr_1@customers.smartfood.dev",
        subject="Your receipt",
        body="Paid in full.",
        attachment_key="receipts/ord_1.pdf",
    )


def test_accepted_send_returns_the_provider_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"message_id": "msg_abc"})

    assert _send(_mailer(handler)) == "msg_abc"
    (request,) = seen
    assert request.url.path == "/mailer/send"
    assert b"receipts/ord_1.pdf" in request.content  # reference, not bytes


def test_5xx_is_unavailable_and_4xx_is_rejected():
    with pytest.raises(MailerUnavailable):
        _send(_mailer(lambda _: httpx.Response(503, json={"error": "melting"})))
    with pytest.raises(MailerRejected):
        _send(_mailer(lambda _: httpx.Response(400, json={"error": "bad recipient"})))


def test_network_failure_is_unavailable():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(MailerUnavailable):
        _send(_mailer(handler))


# ── contacts (identity's internal read) ────────────────────────────


def _contacts(handler) -> HttpContacts:
    return HttpContacts("http://identity.test", transport=httpx.MockTransport(handler))


def test_contact_lookup_speaks_system_identity():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"id": "usr_1", "email": "real@person.dev", "full_name": None}
        )

    assert _contacts(handler).email_for("usr_1") == "real@person.dev"
    (request,) = seen
    assert request.url.path == "/v1/internal/users/usr_1"
    # The whole point of the internal surface: the worker authenticates as
    # a SYSTEM caller, stamped by the shared helper — never a user token.
    assert request.headers["x-auth-role"] == "system"
    assert request.headers["x-internal-caller"] == "notification-worker"


def test_unknown_user_is_poison_and_5xx_is_transient():
    with pytest.raises(UnknownRecipient):
        _contacts(lambda _: httpx.Response(404, json={"error": "not found"})).email_for("usr_x")
    with pytest.raises(ContactsUnavailable):
        _contacts(lambda _: httpx.Response(503, json={"error": "down"})).email_for("usr_1")
    # An unexpected 4xx (403 = broken system headers) is OUR bug, not the
    # recipient's — classified transient so a config fix + retry heals it.
    with pytest.raises(ContactsUnavailable):
        _contacts(lambda _: httpx.Response(403, json={"error": "forbidden"})).email_for("usr_1")


def test_identity_network_failure_is_transient():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ContactsUnavailable):
        _contacts(handler).email_for("usr_1")
