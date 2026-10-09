"""The explanation, assembled (FR-83, FR-86).

Three facts, one verdict, one sentence. The work here is entirely about
what happens when a fact is missing, because that is the difference between
an engine that explains and one that guesses:

- **No timeline** — nothing to explain. The customer gets the hand-off,
  because a stage nobody observed is not a stage.
- **No delivery row** — ordinary before READY. Costs the courier clause.
- **No kitchen load** — an unknown kitchen or an unreachable Inventory
  (FR-85). Costs the congestion clause.

Only the first is fatal, and it is fatal deliberately: every other fact
refines an answer that already exists.

The three reads run concurrently. They are independent — no read's result
selects another — so serialising them would put an avoidable second on the
render path of a customer who is already waiting for food.
"""

import asyncio
from datetime import UTC, datetime

from .domain.explain import resolve
from .domain.ports import KitchenLoadPort
from .domain.render import Explanation, TemplateCache, render, supported


class NotYours(Exception):
    """The asker does not own this order.

    Raised rather than returned so it cannot be mistaken for an
    explanation. The API maps it to the same 404 an unknown order gets —
    not-found and not-yours are one answer here, as everywhere else in this
    repo, because telling a stranger an order exists is the leak.
    """


class ExplainService:
    def __init__(
        self,
        *,
        timelines,
        deliveries,
        kitchen_load: KitchenLoadPort | None = None,
        templates: TemplateCache | None = None,
    ) -> None:
        self._timelines = timelines
        self._deliveries = deliveries
        self._load = kitchen_load
        self._templates = templates or TemplateCache()

    async def explain(
        self, order_id: str, *, user_id: str, locale: str = "en"
    ) -> Explanation | None:
        # Resolved once, here, so the rendered locale and the cache key are
        # the same string. An unsupported one becomes `en` rather than
        # becoming an unbounded key (and, with the rewrite layer, a model
        # call a customer can mint by varying a query parameter).
        locale = supported(locale)
        facts = await self._timelines.timeline(order_id)
        if facts is None:
            return None
        if facts.user_id != user_id:
            raise NotYours

        # Only now, once the order is known to exist and to be theirs, do we
        # ask two other services about it. Fetching first would let anyone
        # with an order id generate load on dispatch and inventory.
        delivery, load = await asyncio.gather(
            self._deliveries.delivery(order_id),
            self._kitchen(facts.restaurant_id),
        )
        verdict = resolve(
            facts.timeline,
            now=datetime.now(UTC),
            delivery=delivery,
            load=load,
            budget=facts.budget,
        )
        # No model anywhere on this path. A deterministic resolver picks the
        # ReasonCode and a template renders it, which is what FR-87 names the
        # floor — the model rewrite that used to sit here was removed as
        # optimisation, not capability.
        return render(verdict, locale=locale, cache=self._templates)

    async def _kitchen(self, restaurant_id: str):
        if self._load is None:
            return None
        return await self._load.load(restaurant_id)
