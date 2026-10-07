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
from datetime import UTC, datetime
from typing import Any

from smartfood_kafka import EventType
from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import AnalyticsRepo, event_values, item_values, view_values

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


GROUP_ITEMS = "analytics.facts.items"


class ItemFactsProjector:
    """OrderPlaced → one row per dish ordered (FR-96).

    Its OWN consumer group on the same topic, not a second write inside
    `FactsProjector`. Two reasons, and the second is the one that matters:
    item facts are a Part B feature and the order facts are what the
    dashboards bill by, so a bug here must not be able to park the batches
    those depend on — the notification split-loop rule, applied to a
    projection instead of a topic. It also means the item facts can be
    rebuilt from the topic's start by resetting one group, without
    replaying the order facts alongside them.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        rows: list[dict[str, Any]] = []
        for event in events:
            # ONLY OrderPlaced carries `items[]`. Every other lifecycle
            # event on this topic skips — forward compatibility on a shared
            # topic, the same rule ViewsProjector follows.
            if str(event.get("event_type", "")) != EventType.ORDER_PLACED:
                continue
            rows.extend(item_values(_payload(event)))
        async with self._sessions() as session:
            await AnalyticsRepo(session).insert_item_facts(rows)
            await session.commit()
        log.info("item facts folded", events=len(events), rows=len(rows))


GROUP_REPOINT = "analytics.brand-repoint"


class BrandRepointHandler:
    """catalog.changes → the branch↔brand relationship, in both the forms
    analytics needs it (ADR-0028).

    It heals NULL brand_id on legacy facts and views, and it RECORDS the
    mapping, which is what lets a brand owner's claim reach an assistant
    citation (FR-98): citations name branches and a claim is normally a
    brand, and the facts carry no brand column to heal.

    NATURALLY idempotent either way — the heal's dedupe is its IS NULL
    predicate and the mapping's is its primary key, so no processed_events
    ledger: replaying the compacted topic into a rebuilt database converges
    to the same rows. That the topic is COMPACTED is also why the mapping
    needs no backfill — every branch catalog knows is on it."""

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
            repo = AnalyticsRepo(session)
            # The mapping first, and unconditionally: a branch whose facts
            # need no healing still has to be reachable from its brand, or
            # a tenant with no legacy rows sees an empty AI dashboard.
            await repo.record_brand(restaurant_id, brand_id, datetime.now(UTC))
            healed = await repo.repoint_brand(restaurant_id, brand_id)
            await session.commit()
        if healed:
            log.info(
                "legacy facts repointed to brand",
                restaurant_id=restaurant_id,
                brand_id=brand_id,
                rows=healed,
            )


GROUP_ASSISTANT = "analytics.assistant.v1"


def assistant_values(payload: dict[str, Any], occurred_at: str) -> dict[str, Any] | None:
    """One interaction fact → its row, or None for a payload we cannot read.

    None rather than an exception on a shapeless payload. This topic is
    first-party and this should never fire, which is exactly why it must
    not park the batch: a KPI projection that stops on one bad row stops
    counting everything, and the number a dashboard shows then silently
    describes a shorter window than its label claims.
    """
    message_id = payload.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return None
    try:
        occurred = datetime.fromisoformat(occurred_at)
    except ValueError:
        # Only `message_id` used to be guarded, so the docstring's promise
        # held for exactly one field. An envelope missing `occurred_at`
        # yields "" here and `fromisoformat("")` raises straight out of
        # `handle_batch` — taking the GOOD facts in the same batch with it
        # and stopping the group. Every coercion below is guarded for the
        # same reason; the contract is "skip the row", not "skip one shape
        # of row".
        return None
    try:
        values = {
            "message_id": message_id,
            # `or ""` and not `.get(key, "")`: the default only fires when the
            # KEY IS ABSENT, so a payload carrying an explicit null produced the
            # literal string "None" — which passes every `user_id != ""` guard
            # downstream, making two different null-user turns look like the
            # same customer and joining them to any order row with the same
            # stringified null.
            "conversation_id": str(payload.get("conversation_id") or ""),
            "user_id": str(payload.get("user_id") or ""),
            "city": str(payload.get("city") or ""),
            "outcome": str(payload.get("outcome", "")),
            "refusal_reason": str(payload.get("refusal_reason", "none")),
            "cache_tier": str(payload.get("cache_tier", "")),
            "item_ids": [str(i) for i in payload.get("item_ids") or []],
            "restaurant_ids": [str(r) for r in payload.get("restaurant_ids") or []],
            "candidates": int(payload.get("candidates") or 0),
            "ungrounded": int(payload.get("ungrounded") or 0),
            "duration_ms": float(payload.get("duration_ms") or 0.0),
            "occurred_at": occurred,
        }
    except (TypeError, ValueError):
        return None
    return values


class AssistantFactsProjector:
    """`c1.assistant.events` → one fact row per interaction (FR-94).

    Published through the outbox since B3 and read by nobody until now.
    It is a KPI rather than telemetry (ADR-0044), which is why it rode the
    outbox at all — and why this projector exists on its own consumer
    group: the six FR-95 metrics and FR-97's conversion both start here,
    and a bug in either must not park the batches the order dashboards
    bill by.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        rows: list[dict[str, Any]] = []
        unreadable = 0
        for event in events:
            if str(event.get("event_type", "")) != EventType.ASSISTANT_INTERACTION:
                # Forward compatibility on a shared topic — the same rule
                # every other projector here follows.
                continue
            values = assistant_values(_payload(event), str(event.get("occurred_at", "")))
            if values is None:
                unreadable += 1
                continue
            rows.append(values)
        async with self._sessions() as session:
            await AnalyticsRepo(session).insert_assistant_facts(rows)
            await session.commit()
        if unreadable:
            log.warning("assistant facts skipped unreadable payloads", count=unreadable)
        log.info("assistant facts folded", events=len(events), rows=len(rows))
