"""Pricing failures — a taxonomy, because callers map them to DIFFERENT
API codes (api-standards §2):

- PriceChanged       → 409 PRICE_CHANGED   (state drift: client re-confirms)
- RestaurantClosed   → 409 RESTAURANT_CLOSED (state: paused/closed right now)
- ItemUnavailable    → 409 ITEM_UNAVAILABLE  (state: 86'd, deleted, or the
                        menu structurally changed under the selection)
- InvalidSelection   → 422 VALIDATION_FAILED (client bug: the request could
                        never have been valid against this menu)

State problems (409) heal by refreshing the menu; client bugs (422) don't.
"""


class PricingError(Exception):
    pass


class PriceChanged(PricingError):
    """The cart's total moved between the quote the client saw and now.

    Replaced MenuVersionChanged (ADR-0036). The old guard compared
    `restaurants.version`, so ANY menu edit anywhere invalidated EVERY
    in-flight cart — a new category on an unrelated item forced every
    checkout to re-confirm. This compares what the customer actually
    consented to: the total. `current` is the recomputed total in cents."""

    def __init__(self, current: int):
        self.current = current
        super().__init__(f"total is now {current} cents")


class RestaurantClosed(PricingError):
    pass


class ItemUnavailable(PricingError):
    def __init__(self, item_ids: list[str]):
        self.item_ids = item_ids
        super().__init__(f"unavailable items: {item_ids}")


class InvalidSelection(PricingError):
    def __init__(self, details: list[dict[str, str]]):
        self.details = details
        super().__init__(f"invalid selection: {details}")
