"""Domain ports — what the domain needs from the outside world (hexagonal).

Adapters implement these against real infrastructure; tests substitute
fakes; the domain layer stays free of httpx/redis imports.
"""

from contextvars import ContextVar
from typing import Any, Protocol


class GrantError(Exception):
    """Base for grant-port failures."""


class GrantRejected(GrantError):
    """Permanent refusal (identity 4xx) — retrying cannot succeed."""


class GrantUnavailable(GrantError):
    """Transient failure after retries — replaying the onboarding repairs it."""


class GrantsPort(Protocol):
    async def grant_restaurant_admin(self, *, user_id: str, restaurant_id: str) -> None: ...


SEARCH_PATH: ContextVar[str] = ContextVar("search_path", default="lexical")
"""Which `SearchPort` implementation answered THIS request.

Lives here rather than on an adapter for two reasons. The layer contract
forbids `api/` importing `adapters/`, and the route is what stamps the
header — but more importantly a `ContextVar` is request-scoped where an
attribute on the adapter is not: the adapter is built once at startup and
shared by every concurrent request, so one request's fallback could
overwrite another's value between the call and the route reading it.

That matters because the eval suite refuses to score a response not stamped
`hybrid`. A header that can report the wrong path would let the lexical
fallback be graded as the retriever — the exact regression the stamp was
added to prevent. Defaults to `lexical` so an unset value fails closed.
"""


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
    ) -> list[dict[str, Any]]: ...


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
