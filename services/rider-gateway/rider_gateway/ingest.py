"""GPS ingest — one ping in, three Redis writes out (FR-27's shape).

The key spellings MUST match dispatch/adapters/geo.py exactly — the two
services may not import each other (the layer contract), so the schema is
spelled twice with a cross-reference on each side. Drift shows up in the
live stack immediately (candidates stop appearing), never silently.

  sfo:geo:{cell}              GEO set       rider positions
  sfo:loc:{cell}:{rider}      "lat,lon"     TTL 30s — the freshest fix
  sfo:hb:{cell}:{rider}       "1"           TTL 90s — proof of life

Downsampling: every ping refreshes Redis (dispatch needs freshness);
every Nth ping ALSO rides Kafka `rider.locations` — telemetry for
analytics and history, direct-produced like browse events (nothing
transactional to be atomic with, loss tolerated).
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from smartfood_kafka import DOMAIN_EVENT_SCHEMA, DOMAIN_EVENT_SUBJECT, EventType
from smartfood_otel import get_logger

log = get_logger("rider-gateway.ingest")

LOC_TTL_S = 30
HB_TTL_S = 90

_NAMESPACE = uuid.UUID("2e8b4f6a-1c3d-4a75-9e08-5b7d2f9c1a64")


def geo_key(cell: str) -> str:
    return f"sfo:geo:{cell}"


def loc_key(cell: str, rider_id: str) -> str:
    return f"sfo:loc:{cell}:{rider_id}"


def hb_key(cell: str, rider_id: str) -> str:
    return f"sfo:hb:{cell}:{rider_id}"


class LocationSender(Protocol):
    async def send_nowait(
        self,
        topic: str,
        *,
        subject: str,
        schema: dict[str, Any],
        key: str,
        record: dict[str, Any],
    ) -> None: ...


class LocationIngest:
    def __init__(
        self,
        redis: Any,
        *,
        cell: str,
        producer: LocationSender | None = None,
        topic: str = "",
        sample_every: int = 5,
    ):
        self._r = redis
        self._cell = cell
        self._producer = producer
        self._topic = topic
        self._sample_every = max(1, sample_every)

    async def ping(self, rider_id: str, lat: float, lon: float, *, count: int) -> None:
        """One fix: index + latest + heartbeat, and the Nth rides Kafka.
        `count` is the CONNECTION's ping counter — sampling is per session,
        which keeps the wire rate honest without shared state."""
        pipe = self._r.pipeline(transaction=False)
        pipe.geoadd(geo_key(self._cell), (lon, lat, rider_id))  # redis speaks lon-first
        pipe.set(loc_key(self._cell, rider_id), f"{lat},{lon}", ex=LOC_TTL_S)
        pipe.set(hb_key(self._cell, rider_id), "1", ex=HB_TTL_S)
        await pipe.execute()
        if self._producer is None or count % self._sample_every != 0:
            return
        now = datetime.now(UTC)
        try:
            await self._producer.send_nowait(
                self._topic,
                subject=DOMAIN_EVENT_SUBJECT,
                schema=DOMAIN_EVENT_SCHEMA,
                key=rider_id,
                record={
                    "event_id": str(uuid.uuid5(_NAMESPACE, f"{rider_id}:{now.timestamp():.3f}")),
                    "event_type": str(EventType.RIDER_LOCATION),
                    "aggregate_type": "rider",
                    "aggregate_id": rider_id,
                    "aggregate_version": 0,
                    "occurred_at": now,
                    "cell_id": self._cell,
                    "payload": json.dumps(
                        {"rider_id": rider_id, "lat": lat, "lon": lon, "at": now.isoformat()}
                    ),
                },
            )
        except Exception as exc:
            log.warning("location sample dropped", rider_id=rider_id, error=str(exc))
