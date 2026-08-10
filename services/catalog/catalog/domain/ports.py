"""Domain ports — what the domain needs from the outside world (hexagonal).

Adapters implement these against real infrastructure; tests substitute
fakes; the domain layer stays free of httpx/redis imports.
"""

from typing import Protocol


class GrantError(Exception):
    """Base for grant-port failures."""


class GrantRejected(GrantError):
    """Permanent refusal (identity 4xx) — retrying cannot succeed."""


class GrantUnavailable(GrantError):
    """Transient failure after retries — replaying the onboarding repairs it."""


class GrantsPort(Protocol):
    async def grant_restaurant_admin(self, *, user_id: str, restaurant_id: str) -> None: ...


class SearchPort(Protocol):
    """Ranked discovery (ADR-0019). Returns hits already ranked and paginated:
    [{"restaurant_id", "score", "matched_items": [{id, name, price_cents,
    score}]}] — restaurants matched directly AND restaurants surfaced via
    matching items. Implementations own HOW matching works (PG now,
    OpenSearch behind the same port later); the service owns card assembly."""

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        cuisine: str | None,
        tag: str | None,
        limit: int,
        offset: int,
    ) -> list[dict]: ...


class CachePort(Protocol):
    """Read-accelerator contract : implementations must NEVER raise —
    a broken cache returns misses and swallows writes, so the domain's only
    failure mode is 'slower', never 'down'. acquire_lock returns True when the
    cache is unreachable: with no cache, dedup is moot — just render."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def acquire_lock(self, key: str, ttl_ms: int) -> bool: ...
    async def release_lock(self, key: str) -> None: ...
