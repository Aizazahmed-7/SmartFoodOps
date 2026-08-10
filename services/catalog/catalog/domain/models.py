"""Domain models — plain frozen dataclasses, no pydantic, no SQLAlchemy.

The domain speaks these; the API layer converts them to response DTOs and
the adapters build them from rows. Same convention as identity.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Restaurant:
    id: str
    name: str
    city: str
    cuisines: list[str]
    status: str  # open | paused
    lat: float | None
    lon: float | None
    hours: dict | None
    version: int
