"""Budgets and combinations (FR-76, FR-77).

Pure. The budget rule in particular has to be argued with on paper — FR-76
calls it a HARD predicate, and a predicate that is right on average is not
one.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def minimum_total(item: Mapping[str, Any]) -> int:
    """The least this dish can possibly cost, in cents.

    **Not `price_cents`.** A group with `min_select >= 1` must be satisfied
    or the pricing engine refuses the line outright ("group 'Size' requires
    at least 1 selection(s)"), so a dish whose only sizes are +0 and +300 has
    a floor of base+0 — and one whose cheapest required option is +300 has a
    floor of base+300. Budgeting on the base price would promise a total the
    customer cannot actually reach, which is the precise failure FR-76's
    "hard predicate" exists to rule out.

    Optional groups contribute nothing: the customer can decline them, so
    they cannot raise the floor.

    Mirrors `smartfood_pricing.engine`'s rule — `unit = price_cents + sum of
    chosen deltas` — with "chosen" being the cheapest legal selection.
    """
    floor = int(item.get("price_cents", 0))
    groups: Sequence[Mapping[str, Any]] = item.get("modifier_groups") or ()
    for group in groups:
        required = int(group.get("min_select", 0))
        if required <= 0:
            continue
        options: Sequence[Mapping[str, Any]] = group.get("options") or ()
        deltas = sorted(int(o.get("price_delta_cents", 0)) for o in options)
        # A required group with no options cannot be satisfied at all. That
        # is a catalog data bug, not a price — charging 0 for it would hide
        # a dish that can never be ordered inside a budget that looks met.
        floor += sum(deltas[:required]) if len(deltas) >= required else _UNORDERABLE
    return floor


_UNORDERABLE = 10**9
"""Effectively infinite, so an unsatisfiable dish fails every budget rather
than passing every one. Chosen over raising: this runs over a whole city's
menu, and one malformed row must cost that dish, not the recommendation."""


def affordable(cards: Sequence[Mapping[str, Any]], budget_cents: int) -> list[Mapping[str, Any]]:
    """The priced dishes a customer could actually order for the money.

    Takes CARDS and reads `min_total_cents`, rather than recomputing the
    floor. That distinction is the whole rule: a card carries a price and no
    `modifier_groups`, so recomputing would find no required options, fall
    back to the base price, and pass exactly the dishes FR-76 exists to
    exclude. It did, on this function's first outing — the floor is computed
    once where the modifier groups still exist (`cards.py`) and is a fact
    from then on.

    Inclusive: "under ten pounds" allows ten pounds, and an exclusive bound
    would drop the dish priced at the number the customer said.
    """
    return [card for card in cards if int(card["min_total_cents"]) <= budget_cents]


@dataclass(frozen=True)
class Combo:
    """Two dishes people actually order together, and what they cost.

    Pairs, not larger baskets: the co-order signal for a triple is an order
    of magnitude sparser, and a "combo" nobody has ever bought is a
    suggestion with no evidence behind it. Widening this is a data question,
    not a code one.
    """

    restaurant_id: str
    item_ids: Sequence[str]
    total_cents: int
    orders: int


def combine(
    pairs: Sequence[tuple[str, str, str, int]],
    floors: Mapping[tuple[str, str], int],
    *,
    budget_cents: int | None,
    limit: int,
) -> list[Combo]:
    """Co-ordered pairs that fit, most-ordered first.

    `pairs` is `(restaurant_id, item_a, item_b, orders together)` from the
    order history; `floors` is each dish's minimum total at LIVE prices,
    keyed by `(restaurant_id, item_id)`. The history says what goes together
    and the live snapshot says what it costs — neither alone can answer the
    question, and using the history's own prices would quote a customer
    whatever the menu said last month.

    Keyed by the PAIR and not the item alone, because one `item_id` is
    served by every branch of a brand and a floor resolved from the wrong
    branch prices a dish that branch may not even be serving.

    A pair with a dish we could not price is dropped rather than guessed at
    — and "could not price" now includes sold out and kitchen shut, since a
    combo nobody named is a suggestion we chose to make.
    """
    combos: list[Combo] = []
    for restaurant_id, first, second, orders in pairs:
        left, right = (restaurant_id, first), (restaurant_id, second)
        if left not in floors or right not in floors:
            continue
        total = floors[left] + floors[right]
        if budget_cents is not None and total > budget_cents:
            continue
        combos.append(
            Combo(
                restaurant_id=restaurant_id,
                item_ids=(first, second),
                total_cents=total,
                orders=orders,
            )
        )
    # Most co-ordered first, then cheapest, then by id so the same data
    # always produces the same list.
    combos.sort(key=lambda c: (-c.orders, c.total_cents, c.item_ids[0]))
    return combos[:limit]
