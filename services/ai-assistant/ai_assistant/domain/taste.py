"""What a customer likes, and how that ranks a menu (FR-75).

Content-based, over the tags and cuisines a restaurant DECLARED. Not a
learned embedding of a user, and the reason is the same one that shaped
every other decision in this plane: an answer has to be explainable and
groundable. "You often order spicy Pakistani food" is a sentence with
evidence behind it; a latent vector is not, and neither is anything a
customer could argue with.

Pure. The aggregation and the scoring both run without a database, so
"why is this dish first" is answerable from a profile and an item alone.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

WINDOW = timedelta(days=180)
"""How far back taste looks.

Six months, against popularity's thirty days, because they measure
different things: popularity is "what is happening here now" and taste is
"who this person is". Someone who ordered biryani weekly all spring is
still a biryani person in July.
"""

MIN_ORDERS = 2
"""Below this, a profile is not a profile.

One order is a data point, not a preference — and FR-75's whole claim is
that a personalised list DIFFERS from the popularity baseline. Building a
"personalised" list off a single order would produce a difference with
nothing behind it, which is worse than the baseline because it looks like
it knows something.
"""

CUISINE_WEIGHT = 1.0
TAG_WEIGHT = 0.6
"""Cuisine outranks tag, because cuisine is the coarser and more reliable
signal. A customer who orders Pakistani food will keep ordering it; a
`spicy` tag is one restaurant's opinion about one dish, and different
kitchens mean different things by it."""

FAMILIAR_WEIGHT = 0.3
"""A small nudge toward restaurants they already order from — enough to
break a tie, not enough to trap them in one kitchen. A recommender that
only ever returns the place you order from is a bookmark."""

NOVELTY_WEIGHT = 0.25
"""A nudge the other way, toward a dish they have NOT had.

Both nudges exist because a food recommender has two jobs that pull apart:
people reorder favourites constantly, and a list of things you have already
eaten teaches you nothing. Rather than excluding what they have ordered —
which would hide the one dish they actually want — a familiar dish has to
earn its place on taste alone, and an unfamiliar one of equal fit edges it.
"""


@dataclass(frozen=True)
class Taste:
    """A customer's declared-attribute preferences, as counts.

    Counts rather than normalised weights: the raw numbers are what makes
    the profile legible in a database, and normalisation is a scoring
    concern that belongs where scoring happens.
    """

    cuisines: Mapping[str, int] = field(default_factory=lambda: {})
    tags: Mapping[str, int] = field(default_factory=lambda: {})
    restaurants: Mapping[str, int] = field(default_factory=lambda: {})
    ordered: Sequence[str] = field(default_factory=lambda: ())
    orders: int = 0

    @property
    def thin(self) -> bool:
        return self.orders < MIN_ORDERS


@dataclass(frozen=True)
class Attributes:
    """One candidate dish, as the attributes a profile can match."""

    item_id: str
    restaurant_id: str
    cuisines: Sequence[str] = field(default_factory=lambda: ())
    tags: Sequence[str] = field(default_factory=lambda: ())


def build(rows: Iterable[tuple[str, str, str, Sequence[str], Sequence[str], int]]) -> Taste:
    """Fold a customer's order history into a profile.

    Rows are `(order_id, item_id, restaurant_id, cuisines, tags, qty)` — one
    per dish ordered, already windowed by the caller.

    **Weighted by ORDERS, not by quantity.** Buying four naans on one
    evening is one decision about naan, and letting quantity in would make a
    catering order rewrite somebody's taste — the same reason popularity
    ranks on distinct orders (FR-80).

    `orders` counts DISTINCT order ids, which it did not until the B4
    review: it counted rows, so one order containing two dishes reported
    `orders = 2` and cleared `MIN_ORDERS`. That shipped precisely what the
    constant exists to prevent — a personalised list built on a single
    order, differing from the baseline with nothing behind the difference.
    Both tests that should have caught it used one-dish orders.
    """
    cuisines: dict[str, int] = {}
    tags: dict[str, int] = {}
    restaurants: dict[str, int] = {}
    ordered: list[str] = []
    seen_orders: set[str] = set()
    for order_id, item_id, restaurant_id, item_cuisines, item_tags, _qty in rows:
        seen_orders.add(order_id)
        if item_id not in ordered:
            ordered.append(item_id)
        restaurants[restaurant_id] = restaurants.get(restaurant_id, 0) + 1
        for cuisine in item_cuisines:
            cuisines[cuisine] = cuisines.get(cuisine, 0) + 1
        for tag in item_tags:
            tags[tag] = tags.get(tag, 0) + 1
    return Taste(
        cuisines=cuisines,
        tags=tags,
        restaurants=restaurants,
        ordered=tuple(ordered),
        orders=len(seen_orders),
    )


def score(profile: Taste, item: Attributes) -> float:
    """How well one dish fits one customer.

    Each component is normalised against the profile's own maximum, so a
    customer with two hundred orders and one with five are scored on the
    same scale — otherwise the heaviest user's scores would dwarf everyone
    else's and any threshold tuned on one would be wrong for the other.
    """
    return (
        CUISINE_WEIGHT * _affinity(profile.cuisines, item.cuisines)
        + TAG_WEIGHT * _affinity(profile.tags, item.tags)
        + FAMILIAR_WEIGHT * _affinity(profile.restaurants, [item.restaurant_id])
        + (NOVELTY_WEIGHT if item.item_id not in profile.ordered else 0.0)
    )


def _affinity(counts: Mapping[str, int], attributes: Sequence[str]) -> float:
    """The best match between a dish's attributes and the profile, 0..1.

    BEST, not sum: a dish tagged `spicy, halal, popular` should not outscore
    one tagged `spicy` just for carrying more labels. What is being asked is
    "how much does this customer like the thing this dish most is", and a
    sum answers "how many labels did the restaurant type".
    """
    if not counts or not attributes:
        return 0.0
    top = max(counts.values())
    return max(counts.get(attribute, 0) for attribute in attributes) / top


def recommend(profile: Taste, candidates: Sequence[Attributes], limit: int) -> list[Attributes]:
    """The customer's menu, best first.

    Ties break on item id so the same profile and the same menu always
    produce the same list — a recommendation that reshuffles between two
    identical requests looks broken and cannot be debugged.
    """
    return sorted(candidates, key=lambda item: (-score(profile, item), item.item_id))[:limit]
