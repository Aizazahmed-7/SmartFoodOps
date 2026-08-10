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
