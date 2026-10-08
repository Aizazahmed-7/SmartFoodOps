"""Kafka consumers for the GenAI plane.

DoD-2 declarations for `assistant.features.v1` (B4):

**Dedupe mode: NATURAL_KEY.** `(order_id, item_id)` — both derived from the
payload, neither minted — with INSERT .. DO NOTHING. An order's items cannot
change, so a redelivered `OrderPlaced` has nothing to update and must not
pretend otherwise; there is no ledger and none is wanted.

**Ordering: irrelevant.** The topic key is `order_id`, so one order's events
share a partition, but this handler reads only `OrderPlaced` and each row is
written once. Two orders reordered against each other change nothing: the
aggregates are computed at read time by grouping, never by incrementing.

**Handler contract.** It projects and it returns. No provider call, no
embedding, nothing that can stall a partition on somebody else's outage.

DoD-2 declarations for `assistant.knowledge.v1`:

**Dedupe mode: NATURAL_KEY.** Two keys, both derived, neither minted. The
pending row is keyed by `restaurant_id` and upserted, so a redelivered event
overwrites the same row instead of queueing a second pass. The chunks the
drain then writes are keyed by `{restaurant_id}:{item_id}` (ADR-0033), so
the same is true one layer down. There is no `processed_events` ledger and
none is wanted: catalog's payloads are full-state snapshots, which makes
"apply twice" and "apply once" indistinguishable by construction.

**Ordering: strict-per-key, and nothing depends on it.** The topic key is
`aggregate_id` = `restaurant_id`, so one restaurant's events share a
partition and arrive in order. That is a property we get for free rather
than one we rely on: because every payload is a complete snapshot,
last-write-wins is genuinely last-state-wins, and even a reordered pair
converges as soon as the newer event lands.

**Handler contract.** It projects and it returns. The only side effect is a
row in `knowledge_pending`; embedding, the provider call and the index write
all happen in the drain, off this path. That is what keeps a provider
outage from stalling a partition — ADR-0025's rule, and the reason the
offset is committed within milliseconds of the event arriving rather than
after an embedding round trip.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.attribution import AttributionRepo
from .adapters.features import FeatureRepo
from .adapters.repo import PendingRepo
from .domain.knowledge import is_indexable
from .metrics import RECOMMENDATIONS

log = get_logger("ai-assistant.consumers")

GROUP_KNOWLEDGE = "assistant.knowledge.v1"
"""`{service}.{purpose}.v{n}` (DoD-2). Re-consuming the topic from the
beginning means bumping the `v`, never resetting a live group's offsets —
which is exactly how FR-59's rebuild is performed: truncate the tables,
start `v2`, and the compacted topic replays the whole index."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class KnowledgeHandler:
    """`catalog.changes` -> the debounce queue (FR-57).

    Deliberately thin. Everything this handler could get wrong is either a
    pure decision it delegates to `is_indexable` or a single upsert it
    delegates to `PendingRepo`; what is left is the part that has to be read
    carefully anyway — which events are ours, and what happens to the ones
    that are not.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        debounce_s: float,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._debounce_s = debounce_s
        self._clock = clock

    async def handle(self, event: dict[str, Any]) -> None:
        if event.get("aggregate_type") != "restaurant":
            return  # not a catalog restaurant fact
        restaurant_id = str(event["aggregate_id"])
        # A payload that will not parse is NOT swallowed: raising invokes
        # the framework's bounded retry and then parks the original bytes on
        # the DLQ (ADR-0021), where the message is inspectable and
        # replayable. Dropping it would leave a restaurant silently missing
        # from search with nothing to point at.
        payload = json.loads(event["payload"])
        if not is_indexable(payload):
            # A brand's fan-out event, or a branch we cannot place. Returning
            # commits the offset: there is nothing to do and nothing to
            # retry, and queueing work the drain would only discard would
            # make the backlog lie about how stale the index is.
            return
        async with self._sessions() as session:
            await PendingRepo(session).stage(
                restaurant_id=restaurant_id,
                payload=payload,
                now=self._clock(),
                debounce_s=self._debounce_s,
            )
            await session.commit()
        log.info(
            "restaurant queued for reindex",
            restaurant_id=restaurant_id,
            event_type=event.get("event_type"),
        )


GROUP_FEATURES = "assistant.features.v1"
"""The order-history feed behind popularity (FR-80) and taste (FR-75).

A SECOND group on `c1.orders.events`, beside analytics' own two. Analytics
owns the metrics; this owns the features the assistant answers with, and a
synchronous call between them would put another service on the answer path
of every recommendation. Re-consuming from the start means bumping the `v`,
never resetting a live group's offsets.
"""


class FeatureHandler:
    """`OrderPlaced` → one row per dish ordered, and any acceptance it
    settles (FR-79).

    Only OrderPlaced carries `items[]`; every other lifecycle event on this
    shared topic skips, which is the forward-compatibility rule every
    projector here follows — a newer producer must not park this consumer.

    The acceptance join lives HERE and not in a surface, because this is the
    only place that sees what was actually ordered. A client reporting "the
    customer took our suggestion" would be grading its own work.
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, window: timedelta | None = None
    ) -> None:
        self._sessions = sessions
        self._window = window or timedelta(hours=2)

    async def handle(self, event: dict[str, Any]) -> None:
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        rows: list[dict[str, Any]] = []
        for event in events:
            if str(event.get("event_type", "")) != "OrderPlaced":
                continue
            rows.extend(order_rows(_event_payload(event)))
        accepted = 0
        async with self._sessions() as session:
            await FeatureRepo(session).record(rows)
            accepted = await self._attribute(session, rows)
            await session.commit()
        log.info("order features recorded", events=len(events), rows=len(rows), accepted=accepted)

    async def _attribute(self, session: AsyncSession, rows: list[dict[str, Any]]) -> int:
        """Did any of these dishes come from something we suggested?

        Grouped by order, because a showing is accepted by an ORDER — three
        suggested dishes on one receipt is one acceptance of three items,
        not three acceptances.
        """
        repo = AttributionRepo(session)
        by_order: dict[tuple[str, str, datetime], set[str]] = {}
        for row in rows:
            key = (row["order_id"], row["user_id"], row["placed_at"])
            by_order.setdefault(key, set()).add(row["item_id"])

        settled = 0
        for (order_id, user_id, placed_at), ordered in by_order.items():
            if not user_id:
                continue  # nothing to attribute a guest order to
            for shown_id, shown_items in await repo.recent_showings(
                user_id=user_id, before=placed_at, window=self._window
            ):
                taken = sorted(ordered & set(shown_items))
                if not taken:
                    continue
                # `accepted_at` is the ORDER's clock (it is a fact about
                # when the order happened); the outbox stamp is OURS. Staging
                # a row with a foreign, possibly hours-old timestamp made the
                # poller measure the order's age as publish lag and would
                # fire NFR-6's p99<5s alert on a healthy outbox (B4 review).
                if await repo.record_acceptance(
                    shown_id=shown_id,
                    order_id=order_id,
                    item_ids=taken,
                    accepted_at=placed_at,
                    now=_utc_now(),
                ):
                    RECOMMENDATIONS.labels(surface="order", outcome="accepted").inc()
                    settled += 1
        return settled


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """The Avro envelope carries payload as a JSON STRING — the schema stays
    stable while payload shapes evolve. Indexing it like a dict is the
    mistake that once parked analytics' whole topic history."""
    raw = event.get("payload") or {}
    return json.loads(raw) if isinstance(raw, str) else raw


def _view_id(event_id: str, user_id: str) -> str:
    """A view's natural key, salted with the viewer.

    Still derived (so a redelivered event collapses onto the same row), but
    no longer forgeable across users: the only client-controlled input is
    now scoped to the client it came from.
    """
    return hashlib.sha256(f"{event_id}|{user_id}".encode()).hexdigest()


def order_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One OrderPlaced → one row per distinct dish, quantities summed.

    Pure, so the convergence guard is testable directly. The cart splits a
    line per option combination, so one order legitimately carries the same
    dish twice — summing is what keeps `(order_id, item_id)` an absolute
    value rather than a row that silently drops a line.
    """
    placed_at = datetime.fromisoformat(payload["placed_at"])
    merged: dict[str, dict[str, Any]] = {}
    for line in payload.get("items") or []:
        item_id = line.get("menu_item_id")
        if not item_id:
            # Unusable to anything that keys on item ids, and parking the
            # partition over one malformed line would cost every order
            # behind it.
            continue
        row = merged.get(item_id)
        if row is None:
            merged[item_id] = {
                "order_id": payload["order_id"],
                "item_id": item_id,
                "restaurant_id": payload["restaurant_id"],
                "user_id": payload.get("user_id", ""),
                "qty": int(line.get("qty", 0)),
                "placed_at": placed_at,
            }
            continue
        row["qty"] += int(line.get("qty", 0))
    return list(merged.values())


GROUP_VIEWS = "assistant.views.v1"
"""Browsing telemetry, for the familiarity half of a taste profile (FR-75).

Its OWN loop, beside the features one. `browse.events` is high-volume and
lossy by design, and a backlog of it must never sit in front of the order
history that carries the actual preferences — the notification split-loop
rule, which this service has now applied three times.
"""


class ViewHandler:
    """`MenuViewed` → one row per identified view.

    **Anonymous views are dropped**, not stored with a null user. A profile
    needs somebody to belong to, and a table of views that cannot be
    attributed is a retention obligation with no reader (NFR-32).
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        rows: list[dict[str, Any]] = []
        for event in events:
            if str(event.get("event_type", "")) != "MenuViewed":
                continue
            payload = _event_payload(event)
            user_id = payload.get("user_id")
            if not user_id:
                continue
            rows.append(
                {
                    # Derived from the EVENT's own id and the viewer, not
                    # from anything a payload offers. `event_id` is
                    # `uuid5(namespace, request_id)` and `X-Request-ID` is a
                    # client-supplied header the edge forwards — so a caller
                    # reusing one value collapsed all of its views into one
                    # row, and could pre-insert an id to suppress somebody
                    # else's (B4 review). Salting with the user makes a
                    # collision affect only the colliding user.
                    "view_id": _view_id(str(event.get("event_id", "")), user_id),
                    "restaurant_id": payload["restaurant_id"],
                    "user_id": user_id,
                    "viewed_at": datetime.fromisoformat(payload["viewed_at"]),
                }
            )
        async with self._sessions() as session:
            await FeatureRepo(session).record_views(rows)
            await session.commit()
        log.info("views recorded", events=len(events), rows=len(rows))
