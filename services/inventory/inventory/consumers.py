"""Stock provisioning — inventory's handler on the shared consumer runtime.

StockProvisioningHandler makes STRICT stock livable: every catalog change
event carries the restaurant's FULL menu snapshot, so on any event we upsert
a zero-stock row for every item we have never seen, plus a default capacity
row for the restaurant. Existing stock is NEVER touched — an admin's counts
survive every menu edit.

The loop itself is smartfood_kafka.EventConsumer (ADR-0021); this module is
only the handler. Dedupe mode: NATURAL_KEY (DoD-2) — a create-if-absent
upsert makes replaying any event a no-op by construction, so no
processed_events table (identity needed one because grants are not
naturally idempotent).
"""

import json
from datetime import UTC, datetime
from typing import Any

from smartfood_otel import get_logger
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

        if payload.get("kind") == "brand":
            # Brand aggregates are menu TEMPLATES (ADR-0028): they carry the
            # base menu but own no fridge and no kitchen. Provisioning them
            # would mint phantom (brd_, item) stock rows and a phantom
            # capacity row — the branches' own events carry the same items
            # with the id that actually sells them.
            return

        async with self._sessions() as session:
            repo = InventoryRepo(session)
            known = {row.item_id for row in await repo.list_stock(restaurant_id)}
            for item_id in item_ids:
                if item_id in known:
                    continue  # existing counts survive menu edits
                # Conflict-safe: a racing admin PUT wins WITHOUT the old
                # mid-batch rollback that silently discarded every earlier
                # provisioned row of this same event.
                await repo.insert_stock(restaurant_id, item_id, 0, _now())
            if await repo.get_load(restaurant_id) is None:
                await repo.insert_load(restaurant_id, self._default_capacity)
            await session.commit()
            log.info(
                "stock rows provisioned",
                restaurant_id=restaurant_id,
                new_items=len([i for i in item_ids if i not in known]),
            )
