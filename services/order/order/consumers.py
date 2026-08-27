"""catalog.changes → orders.brand_id backfill (ADR-0028).

Legacy orders were placed before their restaurant had a brand; the brands
cutover emits one fresh full-state event per branch carrying brand_id, and
this handler folds it in. NATURALLY idempotent — the WHERE is the dedupe
(`brand_id IS NULL`), so no processed_events ledger: replaying any event,
or the whole compacted topic into a rebuilt database, converges to the
same rows. New orders stamp brand_id at placement and never match.

The loop that feeds this handler is smartfood_kafka.EventConsumer,
tested in its own lib.
"""

import json
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .domain.transitions import repoint_brand

log = get_logger("order.consumers")

GROUP = "order.brand-repoint"


class BrandRepointHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        payload = json.loads(event["payload"])
        brand_id = payload.get("brand_id")
        if event.get("aggregate_type") != "restaurant" or not brand_id:
            return  # not a restaurant fact, or a pre-brands payload
        restaurant_id = str(event["aggregate_id"])
        if restaurant_id == brand_id:
            return  # the brand's own aggregate — orders reference branches
        healed = await repoint_brand(self._sessions, restaurant_id, brand_id)
        if healed:
            log.info(
                "legacy orders repointed to brand",
                restaurant_id=restaurant_id,
                brand_id=brand_id,
                rows=healed,
            )
