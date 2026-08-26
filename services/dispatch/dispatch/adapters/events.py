"""dispatch.events — direct to Kafka, no outbox (ADR-0026).

The browse-telemetry posture, for the same structural reason: dispatch's
truth lives in DynamoDB, so there is no SQL transaction for an outbox row
to join — the conditional write either happened or it didn't, and Kafka
carries a COPY for analytics (rider utilization) and any future consumer.
fire-and-forget + no-raise: a Kafka blip costs a metric a data point,
never an assignment. Prod closes the at-most-once gap with DDB Streams.

Identity: event_id = uuid5(kind:subject:marker) — deterministic per FACT
(the assignment of ord_42 to r1 under offer off_9 is the same fact on
every retry), so analytics' PK dedupe absorbs redelivery.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from smartfood_kafka import DOMAIN_EVENT_SCHEMA, DOMAIN_EVENT_SUBJECT, EventType
from smartfood_otel import get_logger

log = get_logger("dispatch.events")

_NAMESPACE = uuid.UUID("9f3d2b7c-5a41-4e96-8d2f-b7c61a0e4d53")


class DomainEventSender(Protocol):
    """Structural producer seam (the smartfood-outbox lesson): depend on
    send_nowait, not on the concrete EventProducer."""

    async def send_nowait(
        self,
        topic: str,
        *,
        subject: str,
        schema: dict[str, Any],
        key: str,
        record: dict[str, Any],
    ) -> None: ...


class DispatchEvents:
    def __init__(self, producer: DomainEventSender | None, *, topic: str, cell_id: str):
        # None = disarmed (unit tests, kafka-less dev) — every emit no-ops.
        self._producer = producer
        self._topic = topic
        self._cell_id = cell_id

    async def _emit(
        self,
        event_type: EventType,
        *,
        aggregate_type: str,
        aggregate_id: str,
        marker: str,
        payload: dict[str, Any],
    ) -> None:
        if self._producer is None:
            return
        now = datetime.now(UTC)
        try:
            await self._producer.send_nowait(
                self._topic,
                subject=DOMAIN_EVENT_SUBJECT,
                schema=DOMAIN_EVENT_SCHEMA,
                key=aggregate_id,
                record={
                    "event_id": str(
                        uuid.uuid5(_NAMESPACE, f"{event_type}:{aggregate_id}:{marker}")
                    ),
                    "event_type": str(event_type),
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "aggregate_version": 0,
                    "occurred_at": now,
                    "cell_id": self._cell_id,
                    "payload": json.dumps({**payload, "occurred_at": now.isoformat()}),
                },
            )
        except Exception as exc:
            log.warning("dispatch event dropped", event_type=str(event_type), error=str(exc))

    async def rider_online(self, rider_id: str, *, session_marker: str) -> None:
        await self._emit(
            EventType.RIDER_ONLINE,
            aggregate_type="rider",
            aggregate_id=rider_id,
            marker=session_marker,
            payload={"rider_id": rider_id},
        )

    async def rider_offline(self, rider_id: str, *, session_marker: str) -> None:
        await self._emit(
            EventType.RIDER_OFFLINE,
            aggregate_type="rider",
            aggregate_id=rider_id,
            marker=session_marker,
            payload={"rider_id": rider_id},
        )

    async def rider_assigned(self, order_id: str, *, rider_id: str, offer_id: str) -> None:
        await self._emit(
            EventType.RIDER_ASSIGNED,
            aggregate_type="delivery",
            aggregate_id=order_id,
            marker=offer_id,
            payload={"order_id": order_id, "rider_id": rider_id},
        )

    async def delivery_completed(self, order_id: str, *, rider_id: str) -> None:
        await self._emit(
            EventType.RIDER_DELIVERY_COMPLETED,
            aggregate_type="delivery",
            aggregate_id=order_id,
            marker="done",
            payload={"order_id": order_id, "rider_id": rider_id},
        )
