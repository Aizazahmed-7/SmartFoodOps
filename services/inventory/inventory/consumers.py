"""Kafka consumer — inventory's asynchronous inbound edge.

StockProvisioningHandler makes STRICT stock livable: every catalog change
event carries the restaurant's FULL menu snapshot, so on any event we upsert
a zero-stock row for every item we have never seen, plus a default capacity
row for the restaurant. Existing stock is NEVER touched — an admin's counts
survive every menu edit.

Dedupe mode: NATURAL_KEY (DoD-2). The handler is a create-if-absent upsert —
replaying any event is a no-op by construction, so no processed_events table
(identity needed one because grants are not naturally idempotent).
"""

import json
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from smartfood_otel import get_logger, trace_id_of
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import InventoryRepo

log = get_logger("inventory.consumers")

GROUP = "inventory.stock-provisioning"


def _now() -> datetime:
    return datetime.now(UTC)


class StockProvisioningHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, default_capacity: int = 10):
        self._sessions = sessions
        self._default_capacity = default_capacity

    async def handle(self, event: dict[str, Any]) -> None:
        try:
            payload = json.loads(event["payload"])
            restaurant_id = str(event["aggregate_id"])
            item_ids = [
                item["id"]
                for category in payload["menu"]["categories"]
                for item in category["items"]
            ]
        except (KeyError, TypeError, ValueError):
            # Not a menu-bearing catalog event (or malformed) — nothing to
            # provision. Log and move on; never poison the partition.
            log.warning("unprovisionable catalog event", event_id=str(event.get("event_id")))
            return

        async with self._sessions() as session:
            repo = InventoryRepo(session)
            known = {row.item_id for row in await repo.list_stock(restaurant_id)}
            for item_id in item_ids:
                if item_id in known:
                    continue  # existing counts survive menu edits
                try:
                    await repo.insert_stock(restaurant_id, item_id, 0, _now())
                except IntegrityError:  # racing admin PUT — theirs wins
                    await session.rollback()
            if await repo.get_load(restaurant_id) is None:
                try:
                    await repo.insert_load(restaurant_id, self._default_capacity)
                except IntegrityError:
                    await session.rollback()
            await session.commit()
            log.info(
                "stock rows provisioned",
                restaurant_id=restaurant_id,
                new_items=len([i for i in item_ids if i not in known]),
            )


class _Decoder(Protocol):
    async def decode(self, data: bytes) -> dict[str, Any]: ...


class _Handler(Protocol):
    async def handle(self, event: dict[str, Any]) -> None: ...


class CatalogChangesConsumer:
    """Thin loop around an aiokafka-shaped client (injectable for tests):
    decode → handle → commit offset. Commit AFTER handling = at-least-once.
    Same shape as identity's — extracting a shared lib is a W3 chore."""

    def __init__(self, client: Any, serde: _Decoder, handler: _Handler):
        self._client = client
        self._serde = serde
        self._handler = handler

    async def run(self) -> None:
        await self._client.start()
        try:
            async for message in self._client:
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
