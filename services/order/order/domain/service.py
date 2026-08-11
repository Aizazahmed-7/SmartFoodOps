"""Order domain — S2 scope: the stateless quote. Placement, reads, and the
saga arrive in S3/S5; this file grows with them.

The quote is deliberately snapshot→price with NOTHING in between: no DB,
no cache, no inventory (stock is ValidateAndReserve's concern at placement,
with a real reservation behind it — a quote must never pretend to hold
stock)."""

from typing import Any

from smartfood_pricing import Line, PricedOrder, PricingConfig, price_order

from .ports import CatalogPort


class OrderService:
    def __init__(self, catalog: CatalogPort, *, pricing: PricingConfig):
        self._catalog = catalog
        self._pricing = pricing

    async def quote(self, restaurant_id: str, lines: list[Line]) -> PricedOrder:
        """Price a cart against the CURRENT menu (expected_menu_version=None:
        a quote self-heals across menu edits — the response carries the
        version the client should re-pin its cart to)."""
        snapshot = await self._snapshot(restaurant_id, lines)
        return price_order(snapshot, lines, config=self._pricing)

    async def _snapshot(self, restaurant_id: str, lines: list[Line]) -> dict[str, Any]:
        # Dedupe ids, preserving order (two lines of the same item with
        # different options are one snapshot item).
        item_ids: list[str] = []
        for line in lines:
            if line.item_id not in item_ids:
                item_ids.append(line.item_id)
        return await self._catalog.get_snapshot(restaurant_id, item_ids)
