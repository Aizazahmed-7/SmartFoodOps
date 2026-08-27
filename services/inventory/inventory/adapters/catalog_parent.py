"""Branch→brand resolution for the admin surface's ownership check.

Inventory holds no restaurant rows, so "does this branch belong to the
caller's brand?" is catalog's question. A synchronous lookup (not a
catalog.changes-fed table) on purpose: the seed PUTs stock immediately
after item creation, and an event-carried mapping would race the consumer.
Parentage is immutable — a branch never changes brands — so a
process-lifetime memo is correct; the precedent is DispatchClient's pickup
pin cache. Only found rows are memoized: a 404 today may be a branch
created tomorrow.
"""

import httpx

from ..domain.service import CatalogUnavailable


class CatalogParents:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=3.0)
        self._memo: dict[str, str | None] = {}

    async def brand_of(self, restaurant_id: str) -> str | None:
        """The row's brand_id (None for brands/legacy rows AND for unknown
        ids — both fail an equality test against a real brand claim)."""
        if restaurant_id in self._memo:
            return self._memo[restaurant_id]
        try:
            response = await self._http.get(f"{self._base}/v1/restaurants/{restaurant_id}")
        except httpx.HTTPError as exc:
            raise CatalogUnavailable from exc
        if response.status_code == 404:
            return None  # deliberately NOT memoized
        if response.status_code >= 400:
            raise CatalogUnavailable
        brand_id = response.json().get("brand_id")
        self._memo[restaurant_id] = brand_id
        return brand_id
