import threading
from collections.abc import Callable
from typing import Any

import boto3
import pytest
from dispatch.adapters.rider_store import DeliveryStore, RiderStore, ensure_tables
from moto import mock_aws
from moto.dynamodb.models import DynamoDBBackend

# moto (≤ 5.2.3, the current release) applies a conditional UpdateItem as an
# unlocked check-then-apply on the live stored item: under real threads,
# several writers can all pass the same condition before any of them applies,
# so THE DRILL intermittently saw two winners during `make cov` (coverage's
# per-line thread tracing widens the check→apply window enough to lose the
# GIL's accidental protection). Real DynamoDB serializes writes per item —
# the exact guarantee ADR-0011's lock is built on — so the fixture restores
# that contract to the fake. The drills still race real threads through real
# condition evaluation; only the referee is made atomic, as in production.
# Load-bearing for a deterministic suite: do not remove.
_UPDATE_ITEM_LOCK = threading.Lock()


def _serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    def locked(*args: Any, **kwargs: Any) -> Any:
        with _UPDATE_ITEM_LOCK:
            return method(*args, **kwargs)

    return locked


@pytest.fixture()
def ddb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(DynamoDBBackend, "update_item", _serialized(DynamoDBBackend.update_item))
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        ensure_tables(client, rider_state="rider_state", deliveries="deliveries")
        yield client


@pytest.fixture()
def riders(ddb) -> RiderStore:
    return RiderStore(ddb, "rider_state")


@pytest.fixture()
def deliveries(ddb) -> DeliveryStore:
    return DeliveryStore(ddb, "deliveries")
