"""Pricing failures — a taxonomy, because callers map them to DIFFERENT
API codes (api-standards §2):

- MenuVersionChanged → 409 PRICE_CHANGED   (state drift: client re-confirms)
- RestaurantClosed   → 409 RESTAURANT_CLOSED (state: paused/closed right now)
- ItemUnavailable    → 409 ITEM_UNAVAILABLE  (state: 86'd, deleted, or the
                        menu structurally changed under the selection)
- InvalidSelection   → 422 VALIDATION_FAILED (client bug: the request could
                        never have been valid against this menu)

State problems (409) heal by refreshing the menu; client bugs (422) don't.
"""


class PricingError(Exception):
    pass


class MenuVersionChanged(PricingError):
    def __init__(self, current: int):
        self.current = current
        super().__init__(f"menu is now at version {current}")


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
