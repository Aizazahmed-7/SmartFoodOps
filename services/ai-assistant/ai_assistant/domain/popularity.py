"""What people order here, around now (FR-80).

Pure: the time band and the ranking rule are decided here, so "why did this
dish come first" is answerable without a database. The SQL that applies them
lives in the adapter, the same split `retrieval.py` uses.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

WINDOW = timedelta(days=30)
"""How far back popularity looks.

Long enough that a quiet restaurant still ranks, short enough that a dish
taken off the menu in April is not still leading in June. It is also the
bound on how wrong this can be: nothing here notices a menu change, so the
worst case is thirty days of recommending something that stopped existing —
which is why every id returned is re-resolved through the live snapshot
before a customer sees it (FR-60).
"""

BAND = 1
"""Hours either side of "now" that count as the same eating occasion.

±1 makes a three-hour band, which is roughly one meal. Wider and breakfast
starts recommending biryani; narrower and a city with modest volume has no
data at all in most bands — and an empty band is the one thing FR-80 exists
to prevent.
"""


def hours_around(at: datetime, band: int = BAND) -> list[int]:
    """The hours of day that count as "around now", wrapping at midnight.

    Wrapping matters more than it looks: without it, 23:00 and 00:00 — the
    same late-night eating occasion — would see disjoint windows, and the
    hour with the least data would also be the one that got the least help.
    """
    return sorted({(at.hour + offset) % 24 for offset in range(-band, band + 1)})


@dataclass(frozen=True)
class Popular:
    """One dish, and what it is ranked on.

    Both numbers travel because they answer different questions: `orders` is
    how many people chose it, `units` is how many were sold. A catering
    order of forty naans is one person's opinion and forty units.
    """

    item_id: str
    orders: int
    units: int


def rank(rows: Sequence[Popular], limit: int) -> list[Popular]:
    """Most-ordered first, and **by distinct orders, not units**.

    Units would let one bulk order outvote a dish twenty people chose, which
    is the opposite of what a recommendation is for. Units break ties, so a
    dish people buy several of at a time still edges out one they buy singly.
    Then the id, so the same data always ranks the same way — a
    recommendation that reshuffles between two identical requests looks
    broken and cannot be debugged.
    """
    return sorted(rows, key=lambda row: (-row.orders, -row.units, row.item_id))[:limit]
