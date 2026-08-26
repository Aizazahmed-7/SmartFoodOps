"""The DynamoDB stores — where ADR-0011's lock actually lives.

Every mutating operation here is ONE conditional write. That sentence is
the entire correctness story: DDB evaluates a conditional UpdateItem
atomically per item, so "check capacity and take the lock" can never
interleave with a competitor — there is no read-then-write to race.
Methods return bool (condition held / condition failed) and NEVER raise
on a failed condition: losing a race is an ANSWER the caller branches on
(the too_late idiom from the kitchen), not an error.

Two tables (ADR-0007 — uniform-cardinality keys only):

  rider_state   PK rider_id   — status, cap material, offer_lock, the
                                active_deliveries STRING SET, accept stats
  deliveries    PK order_id   — the delivery's state machine + coords,
                                GSI rider-index (rider_id) for "my job"

One refinement to the canonical expression, recorded here because ADR-0011
asks reviews to diff code against it: `active_deliveries` is a String Set
(so finishing a delivery is an atomic DELETE, which a List cannot do), and
DynamoDB deletes an emptied set's attribute entirely — so the capacity
guard reads `attribute_not_exists(active_deliveries) OR
size(active_deliveries) < :cap`. Same semantics, empty-set-safe.

Sync boto3 on purpose: callers that live on an event loop wrap these in
asyncio.to_thread (boto3 clients are thread-safe); tests (moto) and the
concurrency drill call them directly.
"""

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

# The one place the state names are spelled — they cross into deliveries
# rows, API responses, and dispatch.events.
OFFERING = "OFFERING"
ASSIGNED = "ASSIGNED"
PICKED_UP = "PICKED_UP"
DELIVERED = "DELIVERED"
CANCELLED = "CANCELLED"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _condition_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def ensure_tables(client: Any, *, rider_state: str, deliveries: str) -> None:
    """Converge both tables (dev posture — the initdb idiom for DDB).
    Idempotent: an existing table is left exactly as it is."""
    existing = set(client.list_tables()["TableNames"])
    if rider_state not in existing:
        client.create_table(
            TableName=rider_state,
            KeySchema=[{"AttributeName": "rider_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "rider_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    if deliveries not in existing:
        client.create_table(
            TableName=deliveries,
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "order_id", "AttributeType": "S"},
                {"AttributeName": "rider_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "rider-index",
                    "KeySchema": [{"AttributeName": "rider_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    for name in (rider_state, deliveries):
        client.get_waiter("table_exists").wait(TableName=name)


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    """DDB's typed AttributeValues → plain Python, for the shapes we use."""
    out: dict[str, Any] = {}
    for key, typed in item.items():
        if "S" in typed:
            out[key] = typed["S"]
        elif "N" in typed:
            out[key] = int(typed["N"]) if "." not in typed["N"] else float(typed["N"])
        elif "SS" in typed:
            out[key] = sorted(typed["SS"])
        elif "M" in typed:
            out[key] = _plain(typed["M"])
        elif "BOOL" in typed:  # pragma: no cover — no bools stored today
            out[key] = typed["BOOL"]
    return out


class RiderStore:
    """rider_state — presence, capacity, THE LOCK, and accept stats."""

    def __init__(self, client: Any, table: str):
        self._c = client
        self._t = table

    def get(self, rider_id: str) -> dict[str, Any] | None:
        got = self._c.get_item(TableName=self._t, Key={"rider_id": {"S": rider_id}})
        item = got.get("Item")
        return _plain(item) if item else None

    def set_online(self, rider_id: str) -> None:
        """Presence flips freely and idempotently — no condition. Stats
        and the active set are preserved if present, born empty if not."""
        self._c.update_item(
            TableName=self._t,
            Key={"rider_id": {"S": rider_id}},
            UpdateExpression=(
                "SET #st = :online, updated_at = :now, "
                "offers_made = if_not_exists(offers_made, :zero), "
                "offers_accepted = if_not_exists(offers_accepted, :zero)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":online": {"S": "online"},
                ":now": {"S": _now()},
                ":zero": {"N": "0"},
            },
        )

    def set_offline(self, rider_id: str) -> None:
        self._c.update_item(
            TableName=self._t,
            Key={"rider_id": {"S": rider_id}},
            UpdateExpression="SET #st = :offline, updated_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":offline": {"S": "offline"}, ":now": {"S": _now()}},
        )

    def reserve(self, rider_id: str, *, offer_id: str, order_id: str, cap: int) -> bool:
        """THE double-assignment guard (ADR-0011, canonical form): one
        conditional write takes the offer lock AND checks capacity AND
        requires presence. False = the rider was busy/locked/offline —
        the cascade tries the next candidate."""
        try:
            self._c.update_item(
                TableName=self._t,
                Key={"rider_id": {"S": rider_id}},
                UpdateExpression=(
                    "SET offer_lock = :lock, updated_at = :now, "
                    "offers_made = if_not_exists(offers_made, :zero) + :one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(offer_lock) "
                    "AND (attribute_not_exists(active_deliveries) "
                    "OR size(active_deliveries) < :cap) "
                    "AND #st = :online"
                ),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":lock": {
                        "M": {
                            "offer_id": {"S": offer_id},
                            "order_id": {"S": order_id},
                            "locked_at": {"S": _now()},
                        }
                    },
                    ":cap": {"N": str(cap)},
                    ":online": {"S": "online"},
                    ":now": {"S": _now()},
                    ":zero": {"N": "0"},
                    ":one": {"N": "1"},
                },
            )
            return True
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise

    def accept(self, rider_id: str, *, offer_id: str, order_id: str) -> bool:
        """Lock → assignment, one write: the offer becomes an active
        delivery only if THIS offer still holds the lock. A late accept
        (lock already released or re-issued) answers False, never
        half-applies."""
        try:
            self._c.update_item(
                TableName=self._t,
                Key={"rider_id": {"S": rider_id}},
                UpdateExpression=(
                    "REMOVE offer_lock "
                    "ADD active_deliveries :order "
                    "SET offers_accepted = if_not_exists(offers_accepted, :zero) + :one, "
                    "updated_at = :now"
                ),
                ConditionExpression="offer_lock.offer_id = :oid",
                ExpressionAttributeValues={
                    ":order": {"SS": [order_id]},
                    ":oid": {"S": offer_id},
                    ":zero": {"N": "0"},
                    ":one": {"N": "1"},
                    ":now": {"S": _now()},
                },
            )
            return True
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise

    def release_offer(self, rider_id: str, *, offer_id: str) -> bool:
        """The expiry path: drop the lock IF this offer still holds it.
        False = the accept already converted it (the race the workflow
        resolves by reading the deliveries row)."""
        try:
            self._c.update_item(
                TableName=self._t,
                Key={"rider_id": {"S": rider_id}},
                UpdateExpression="REMOVE offer_lock SET updated_at = :now",
                ConditionExpression="offer_lock.offer_id = :oid",
                ExpressionAttributeValues={":oid": {"S": offer_id}, ":now": {"S": _now()}},
            )
            return True
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise

    def finish_delivery(self, rider_id: str, *, order_id: str) -> bool:
        """Atomic set-DELETE frees the slot (delivered, cancelled, or
        revoked alike). Idempotent: a replay finds the order gone and
        answers False."""
        try:
            self._c.update_item(
                TableName=self._t,
                Key={"rider_id": {"S": rider_id}},
                UpdateExpression="DELETE active_deliveries :order SET updated_at = :now",
                ConditionExpression="contains(active_deliveries, :oid)",
                ExpressionAttributeValues={
                    ":order": {"SS": [order_id]},
                    ":oid": {"S": order_id},
                    ":now": {"S": _now()},
                },
            )
            return True
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise


class DeliveryStore:
    """deliveries — one item per order, a small guarded state machine.
    Same discipline: every transition is one conditional write."""

    def __init__(self, client: Any, table: str):
        self._c = client
        self._t = table

    def get(self, order_id: str) -> dict[str, Any] | None:
        got = self._c.get_item(TableName=self._t, Key={"order_id": {"S": order_id}})
        item = got.get("Item")
        return _plain(item) if item else None

    def put_offering(
        self,
        order_id: str,
        *,
        rider_id: str,
        offer_id: str,
        user_id: str,
        restaurant_name: str,
        pickup: tuple[float, float],
        dropoff: tuple[float, float],
        attempt: int,
    ) -> bool:
        """Record the live offer. Guarded so a stale re-offer can never
        regress an ASSIGNED (or later) delivery: only a missing row or a
        prior OFFERING may be overwritten."""
        try:
            self._c.update_item(
                TableName=self._t,
                Key={"order_id": {"S": order_id}},
                UpdateExpression=(
                    "SET #st = :offering, rider_id = :rider, offer_id = :oid, "
                    "user_id = :user, restaurant_name = :rname, "
                    "pickup_lat = :plat, pickup_lon = :plon, "
                    "dropoff_lat = :dlat, dropoff_lon = :dlon, "
                    "attempt = :attempt, offered_at = :now"
                ),
                ConditionExpression="attribute_not_exists(#st) OR #st = :offering",
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":offering": {"S": OFFERING},
                    ":rider": {"S": rider_id},
                    ":oid": {"S": offer_id},
                    ":user": {"S": user_id},
                    ":rname": {"S": restaurant_name},
                    ":plat": {"N": str(pickup[0])},
                    ":plon": {"N": str(pickup[1])},
                    ":dlat": {"N": str(dropoff[0])},
                    ":dlon": {"N": str(dropoff[1])},
                    ":attempt": {"N": str(attempt)},
                    ":now": {"S": _now()},
                },
            )
            return True
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise

    def _transition(
        self,
        order_id: str,
        *,
        expected: str,
        target: str,
        stamp: str,
        guard_offer: str | None = None,
        guard_rider: str | None = None,
    ) -> bool:
        condition = "#st = :expected"
        values: dict[str, Any] = {
            ":expected": {"S": expected},
            ":target": {"S": target},
            ":now": {"S": _now()},
        }
        if guard_offer is not None:
            condition += " AND offer_id = :oid"
            values[":oid"] = {"S": guard_offer}
        if guard_rider is not None:
            condition += " AND rider_id = :rider"
            values[":rider"] = {"S": guard_rider}
        try:
            self._c.update_item(
                TableName=self._t,
                Key={"order_id": {"S": order_id}},
                UpdateExpression=f"SET #st = :target, {stamp} = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as exc:
            if _condition_failed(exc):
                return False
            raise

    def assign(self, order_id: str, *, offer_id: str) -> bool:
        return self._transition(
            order_id,
            expected=OFFERING,
            target=ASSIGNED,
            stamp="assigned_at",
            guard_offer=offer_id,
        )

    def mark_picked_up(self, order_id: str, *, rider_id: str) -> bool:
        return self._transition(
            order_id,
            expected=ASSIGNED,
            target=PICKED_UP,
            stamp="picked_up_at",
            guard_rider=rider_id,
        )

    def mark_delivered(self, order_id: str, *, rider_id: str) -> bool:
        return self._transition(
            order_id,
            expected=PICKED_UP,
            target=DELIVERED,
            stamp="delivered_at",
            guard_rider=rider_id,
        )

    def unassign(self, order_id: str, *, rider_id: str) -> bool:
        """The reassignment revoke (ADR-0011): conditional on the EXPECTED
        rider still owning an un-picked-up delivery — a rider who already
        scanned pickup wins the race, and this answers False."""
        return self._transition(
            order_id,
            expected=ASSIGNED,
            target=OFFERING,
            stamp="unassigned_at",
            guard_rider=rider_id,
        )

    def cancel(self, order_id: str) -> bool:
        """Order cancelled while dispatch was working it. Pre-pickup only
        (FR-21) — a PICKED_UP delivery refuses and the caller escalates."""
        for expected in (OFFERING, ASSIGNED):
            if self._transition(
                order_id, expected=expected, target=CANCELLED, stamp="cancelled_at"
            ):
                return True
        return False
