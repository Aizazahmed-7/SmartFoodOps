"""Domain value objects — plain frozen dataclasses, no SQLAlchemy leakage."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StockRow:
    item_id: str
    restaurant_id: str
    available: int
    version: int


@dataclass(frozen=True)
class LoadRow:
    restaurant_id: str
    active: int
    capacity: int


@dataclass(frozen=True)
class ReservationLine:
    item_id: str
    qty: int


@dataclass(frozen=True)
class Reservation:
    order_id: str
    restaurant_id: str
    lines: tuple[ReservationLine, ...]
    status: str  # active | released | consumed | expired
    expires_at: datetime
