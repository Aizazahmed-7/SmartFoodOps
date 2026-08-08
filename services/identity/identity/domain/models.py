"""Domain objects — the vocabulary the business rules speak in.

Deliberately distinct from the API's pydantic models (the wire contract) and
the DB rows (the storage shape). Three thin look-alike classes are not
duplication; they are the layer boundary, written down.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenPairData:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class Profile:
    id: str
    email: str
    role: str
    full_name: str | None
    phone: str | None


@dataclass(frozen=True)
class Address:
    id: str
    label: str
    line1: str
    city: str
    lat: float | None
    lon: float | None
