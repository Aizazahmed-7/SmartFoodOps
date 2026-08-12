"""Kafka consumers — identity's asynchronous inbound edge.

GrantConvergenceHandler is ADR-0020's closure made real: `RestaurantCreated`
on catalog.changes → apply the restaurant_admin grant idempotently. With
this running, a lost synchronous grant can no longer produce a permanent
orphan — the event committed with the restaurant IS the durable to-do.

Delivery is at-least-once (offsets commit AFTER handling); safety comes from
three layers: the processed_events dedupe, the grant's own idempotency, and
deterministic event ids. Permanently un-appliable grants are logged and
marked processed — the DLQ arrives with the rest of W3.
"""

import json
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from smartfood_kafka import EventType
from smartfood_otel import get_logger, trace_id_of
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import processed_events
from .domain.service import GrantConflict, IdentityService, UnknownUser

log = get_logger("identity.consumers")

GROUP = "identity.grant-convergence"


class GrantConvergenceHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], service: IdentityService):
        self._sessions = sessions
        self._service = service

    async def handle(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != EventType.RESTAURANT_CREATED:
            return  # not ours — other consumers care, we don't
        event_id = str(event["event_id"])
        async with self._sessions() as session:
            seen = (
                await session.execute(
                    sa.select(processed_events).where(
                        (processed_events.c.consumer_group == GROUP)
                        & (processed_events.c.event_id == event_id)
                    )
                )
            ).one_or_none()
        if seen is not None:
            return  # replay of an already-processed delivery

        payload = json.loads(event["payload"])
        try:
            await self._service.grant_restaurant_admin(
                user_id=payload["owner_user_id"],
                restaurant_id=str(event["aggregate_id"]),
            )
            log.info("grant converged from event", event_id=event_id)
        except (GrantConflict, UnknownUser) as exc:
            # Permanently un-appliable: mark processed so it can't poison the
            # partition; surfaced for ops (proper DLQ lands with W3).
            log.error(
                "grant convergence permanently un-appliable — ops review",
                event_id=event_id,
                error=type(exc).__name__,
            )

        await self._mark_processed(event_id)

    async def _mark_processed(self, event_id: str) -> None:
        async with self._sessions() as session:
            try:
                await session.execute(
                    processed_events.insert().values(
                        consumer_group=GROUP,
                        event_id=event_id,
                        processed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()  # concurrent duplicate delivery — fine


class _Decoder(Protocol):
    async def decode(self, data: bytes) -> dict[str, Any]: ...


class _Handler(Protocol):
    async def handle(self, event: dict[str, Any]) -> None: ...


class CatalogChangesConsumer:
    """Thin loop around an aiokafka-shaped client (injectable for tests):
    decode → handle → commit offset. Committing AFTER handling is what makes
    delivery at-least-once — a crash mid-handle re-delivers."""

    def __init__(self, client: Any, serde: _Decoder, handler: _Handler):
        self._client = client
        self._serde = serde
        self._handler = handler

    async def run(self) -> None:
        await self._client.start()
        try:
            async for message in self._client:
                # Rebind the producer's trace context from the Kafka headers:
                # the consumer's log lines join the SAME trace_id the original
                # HTTP request logged under — grep crosses the async hop.
                structlog.contextvars.clear_contextvars()
                traceparent = next(
                    (v.decode() for k, v in (message.headers or []) if k == "traceparent"),
                    None,
                )
                if traceparent and (trace_id := trace_id_of(traceparent)):
                    structlog.contextvars.bind_contextvars(trace_id=trace_id)
                event = await self._serde.decode(message.value)
                await self._handler.handle(event)
                await self._client.commit()
        finally:
            await self._client.stop()
