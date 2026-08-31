"""The facts projector — analytics' only write path.

ONE micro-batched consumer on c1.orders.events (smartfood_kafka's
run_batches, FR-43): a poll's worth of events folds in ONE transaction with
ONE offset commit. Idempotency is structural — the fold writes absolute
values keyed by order_id, so a redelivered batch (crash before commit, or
the batch runtime's degrade-to-singles pass) converges instead of
double-counting. That structural property is WHY this service may batch at
all: the batch runtime's contract is "handler writes must be idempotent".
Batches land BULK: events fold in Python to one row per order (fold_facts),
then one multi-VALUES upsert per column-set signature — N statements
became ~2-3 per batch; views are one DO NOTHING insert per batch.

No payments loop (unlike notification): every metric here derives from
order events alone, which carry totals and all lifecycle timestamps.
"""

import json
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import AnalyticsRepo, event_values, view_values

log = get_logger("analytics.projector")

GROUP_FACTS = "analytics.facts.orders"
GROUP_VIEWS = "analytics.views.browse"


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """The Avro envelope carries payload as a JSON STRING (the schema stays
    stable while payload shapes evolve). Caught live: indexing it like a
    dict parked the entire topic history — the batch runtime degraded to
    singles and DLQ'd every event, exactly as designed, while this line
    was wrong. Unparseable payloads raise → retry → park, with forensics;
    that is the correct fate for a truly bad one."""
    raw = event.get("payload") or {}
    return json.loads(raw) if isinstance(raw, str) else raw


def fold_facts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge a batch's events into ONE row per order, in batch order — the
    in-Python mirror of what sequential upserts did (later events overwrite
    shared columns, add their own). This uniqueness is what makes the bulk
    upsert LEGAL: Postgres refuses one statement whose ON CONFLICT DO
    UPDATE touches a row twice, and a single poll routinely carries an
    order's whole PLACED→…→CONFIRMED run. Pure, so the invariant is
    directly tested (sqlite's laxer upsert would let a broken fold pass
    the behavioral suite unnoticed)."""
    merged: dict[str, dict[str, Any]] = {}
    for event in events:
        values = event_values(str(event.get("event_type", "")), _payload(event))
        if values is None:
            continue  # unknown type — forward compatibility
        merged.setdefault(values["order_id"], {}).update(values)
    return list(merged.values())


class FactsProjector:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        """Per-message mode compatibility (EventHandler): a batch of one.
        The batch runtime's degrade-to-singles pass rides this too — same
        fold, same bulk path, batch size 1."""
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        rows = fold_facts(events)  # parse errors raise BEFORE the tx opens
        async with self._sessions() as session:
            await AnalyticsRepo(session).upsert_facts(rows)
            await session.commit()
        log.info("facts folded", events=len(events), rows=len(rows))


class ViewsProjector:
    """The browse loop's handler (S8): folds MenuViewed into menu_views.
    Separate loop, separate group — the notification split-loop rule: a
    backlog of browse telemetry must never sit in front of the order facts
    the dashboards actually bill by."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        rows = [
            view_values(_payload(event), str(event.get("event_id", "")))
            for event in events
            if str(event.get("event_type", "")) == "MenuViewed"
            # forward compatibility on a shared topic: other types skip
        ]
        async with self._sessions() as session:
            await AnalyticsRepo(session).insert_views(rows)  # ONE statement per batch
            await session.commit()
        log.info("views added", events=len(events), rows=len(rows))


GROUP_REPOINT = "analytics.brand-repoint"


class BrandRepointHandler:
    """catalog.changes → heals NULL brand_id on legacy facts and views
    (ADR-0028). NATURALLY idempotent — the IS NULL predicate is the dedupe,
    so no processed_events ledger: replaying the compacted topic into a
    rebuilt database converges to the same rows."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        payload = json.loads(event["payload"])
        brand_id = payload.get("brand_id")
        if event.get("aggregate_type") != "restaurant" or not brand_id:
            return  # not a restaurant fact, or a pre-brands payload
        restaurant_id = str(event["aggregate_id"])
        if restaurant_id == brand_id:
            return  # the brand's own aggregate — facts reference branches
        async with self._sessions() as session:
            healed = await AnalyticsRepo(session).repoint_brand(restaurant_id, brand_id)
            await session.commit()
        if healed:
            log.info(
                "legacy facts repointed to brand",
                restaurant_id=restaurant_id,
                brand_id=brand_id,
                rows=healed,
            )
