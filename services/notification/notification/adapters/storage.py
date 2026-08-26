"""S3 receipt store — where rendered PDFs live, addressed by key.

Deliberately SYNC (boto3): the only caller is the Celery worker, and
Celery's execution model is synchronous — bringing an async S3 client into
a prefork child would mean running an event loop per task for nothing.

The bucket self-heals: a put that lands NoSuchBucket creates the bucket
and retries once. That removes the init-ordering problem entirely (no
"seed the bucket before the first settle" step for a fresh LocalStack
volume, no race between two workers booting) — create_bucket is idempotent
on both LocalStack and real S3-in-one-region.

Keys are deterministic (`receipts/{order_id}.pdf`), so a re-render from an
at-least-once retry OVERWRITES the same object with the same bytes — the
storage layer absorbs replays the same way the tables do.
"""

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def receipt_key(order_id: str) -> str:
    """The one place the object key is spelled."""
    return f"receipts/{order_id}.pdf"


class S3ReceiptStore:
    def __init__(self, bucket: str, *, endpoint_url: str = "", client: Any | None = None):
        # client is the test seam; endpoint_url="" means real AWS.
        self._bucket = bucket
        self._client = client or boto3.client(  # pragma: no cover — live wiring
            "s3",
            endpoint_url=endpoint_url or None,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )

    def put(self, key: str, data: bytes) -> None:
        try:
            self._put(key, data)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchBucket":
                raise
            self._client.create_bucket(Bucket=self._bucket)
            self._put(key, data)

    def _put(self, key: str, data: bytes) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType="application/pdf"
        )
