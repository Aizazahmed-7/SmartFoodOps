"""Grant convergence — identity's handler on the shared consumer runtime.

GrantConvergenceHandler is ADR-0020's closure made real: ANY restaurant
event on catalog.changes → apply the restaurant_admin grant idempotently
(every full-state payload carries owner_user_id — deliberately type-agnostic
because the topic is compacted and the surviving event per key may be any
of them). With this running, a lost synchronous grant can no longer produce
a permanent orphan — an event committed with the restaurant IS the durable
to-do, and compaction cannot erase the to-do, only rename it.

The loop itself (at-least-once, trace rebinding, retry-then-DLQ) is
smartfood_kafka.EventConsumer (ADR-0021); this module is only the handler.
Safety comes from three layers: the processed_events dedupe, the grant's
own idempotency, and deterministic event ids. Permanently un-appliable
grants are logged and marked processed — parking them would make the DLQ
a queue of things replay can never fix.
"""

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from smartfood_otel import get_logger
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
        # Type-AGNOSTIC on purpose: catalog.changes is COMPACTED, so the one
        # surviving event per restaurant may be ANY of them — a consumer that
        # only reacted to RestaurantCreated would lose its trigger forever if
        # a later menu edit compacted the birth event away. Every payload
        # carries owner_user_id (full-state contract); the grant is
        # idempotent, so converging repeatedly costs one no-op read.
        payload = json.loads(event["payload"])
        owner_user_id = payload.get("owner_user_id")
        if event.get("aggregate_type") != "restaurant" or not owner_user_id:
            return  # not a restaurant fact (or a pre-owner-field replay)
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

        try:
            # The owner's scope is the BRAND when the payload names one
            # (ADR-0028); pre-brands events lack the key and fall back to
            # the aggregate — exactly the old behavior. Branch events carry
            # their brand_id, so converging from ANY surviving compacted
            # event lands the owner on the brand.
            await self._service.grant_restaurant_admin(
                user_id=owner_user_id,
                restaurant_id=str(payload.get("brand_id") or event["aggregate_id"]),
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
