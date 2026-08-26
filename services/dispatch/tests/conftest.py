import boto3
import pytest
from dispatch.adapters.rider_store import DeliveryStore, RiderStore, ensure_tables
from moto import mock_aws


@pytest.fixture()
def ddb():
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
