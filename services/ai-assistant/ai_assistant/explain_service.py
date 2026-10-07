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
from dataclasses import replace
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
        self._warming: set[asyncio.Task[None]] = set()
        self._drain_timeout_s = 5.0

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
        explanation = render(verdict, locale=locale, cache=self._templates)

        # AFTER the answer exists, and never awaited into it. A model
        # rewrite changes how this reads and nothing about what it says, so
        # charging a waiting customer the latency of one would be paying for
        # the wrong thing. The next reader in the same situation gets it.
        warm = getattr(self._templates, "warm", None)
        polished = getattr(self._templates, "polished", None)
        # Not for a fallback. `source="fallback"` means the chosen copy
        # could not be filled in, and it is the ONLY signal that a template
        # is broken — the customer sees the same hand-off sentence a
        # genuine UNKNOWN produces. Relabelling it "model" would erase the
        # one thing that makes the defect findable, and rewriting copy that
        # was never used is wasted spend.
        if warm is not None and explanation.source != "fallback":
            self._spawn(warm(verdict.reason, locale, explanation.bucket))
            if polished is not None and polished(verdict.reason, locale, explanation.bucket):
                explanation = replace(explanation, source="model")
        return explanation

    async def drain(self) -> None:
        """Let in-flight rewrites finish, then stop.

        The lifespan drains the chat plane for the same reason and this was
        not extended to it: without this, shutdown closes the shared http
        client and the DB engine out from under live warm tasks, and the
        loop reports "Task was destroyed but it is pending!" on every
        deploy. Bounded because each warm is bounded by the router's own
        timeouts; cancelled if it is not, which `warm()` handles by giving
        the key its attempt back.
        """
        if not self._warming:
            return
        done, pending = await asyncio.wait(set(self._warming), timeout=self._drain_timeout_s)
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)

    def _spawn(self, coro) -> None:
        """Fire and forget, with the reference held.

        asyncio only holds a WEAK reference to a running task, so a task
        nobody keeps can be collected mid-await and vanish silently. The
        set is what stops that; discarding on completion is what stops the
        set from being a leak.
        """
        task = asyncio.create_task(coro)
        self._warming.add(task)
        task.add_done_callback(self._warming.discard)

    async def _kitchen(self, restaurant_id: str):
        if self._load is None:
            return None
        return await self._load.load(restaurant_id)
