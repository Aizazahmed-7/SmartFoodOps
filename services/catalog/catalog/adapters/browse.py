"""Browse telemetry — MenuViewed, straight to Kafka, no outbox (S8).

The deliberate asymmetry with every other event in this codebase: order
events ride the outbox because a business fact must be atomic with the
write that made it true. A menu VIEW has no companion write — there is
nothing to be atomic with — and browse volume runs orders of magnitude
above order volume, so outboxing views would double the read path's write
load to protect nothing. Telemetry earns different rules:

  * fire-and-forget (send_nowait): the menu response NEVER waits on Kafka;
  * no-raise: a Kafka blip costs data points, never a customer's menu;
  * sampled: the knob that keeps this honest at real traffic — conversion
    is a RATE, and rates survive sampling (documented on the metric).

Identity: event_id = uuid5(request_id) — deterministic per REQUEST, so a
redelivered event collapses on the consumer's PK while two real views stay
two rows. The viewer may be anonymous (public_read menus): user_id rides
as null, and the funnel counts those toward volume but not conversion —
you cannot join an order to a browser you cannot name.
"""

import json
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from smartfood_kafka import DOMAIN_EVENT_SCHEMA, DOMAIN_EVENT_SUBJECT, EventProducer
from smartfood_otel import get_logger

log = get_logger("catalog.browse")

# Fixed namespace: the dedupe key's stability IS the dedupe (never change it).
_NAMESPACE = uuid.UUID("7c2f5a1d-9e4b-4c83-b1a6-3d8e0f6c2a91")


class BrowseEvents:
    def __init__(
        self,
        producer: EventProducer,
        *,
        topic: str,
        cell_id: str,
        sample_rate: float = 1.0,
        rng: Callable[[], float] = random.random,
    ):
        self._producer = producer
        self._topic = topic
        self._cell_id = cell_id
        self._sample_rate = sample_rate
        self._rng = rng

    async def menu_viewed(
        self,
        restaurant_id: str,
        *,
        user_id: str | None,
        request_id: str,
        brand_id: str | None = None,
    ) -> None:
        if self._rng() >= self._sample_rate:
            return
        now = datetime.now(UTC)
        try:
            await self._producer.send_nowait(
                self._topic,
                subject=DOMAIN_EVENT_SUBJECT,
                schema=DOMAIN_EVENT_SCHEMA,
                key=restaurant_id,
                record={
                    "event_id": str(uuid.uuid5(_NAMESPACE, request_id)),
                    "event_type": "MenuViewed",
                    "aggregate_type": "browse",
                    "aggregate_id": restaurant_id,
                    "aggregate_version": 0,
                    "occurred_at": now,
                    "cell_id": self._cell_id,
                    "payload": json.dumps(
                        {
                            "restaurant_id": restaurant_id,
                            "brand_id": brand_id,
                            "user_id": user_id,
                            "viewed_at": now.isoformat(),
                        }
                    ),
                },
            )
        except Exception as exc:
            # Loss degrades, never corrupts — and never surfaces: a view is
            # the one event whose absence harms nobody's order.
            log.warning("menu view drop", restaurant_id=restaurant_id, error=str(exc))
