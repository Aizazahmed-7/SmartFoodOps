"""smartfood-pricing — ONE pricing implementation for quotes and money.

ADR-0015: pricing is a library, not a service. The stateless /v1/quote and
the placement transaction's immutable pricing snapshot call the same
`price_order`, so a displayed estimate and a charged total can never be
computed by different code.
"""

from .engine import price_order
from .errors import (
    InvalidSelection,
    ItemUnavailable,
    MenuVersionChanged,
    PricingError,
    RestaurantClosed,
)
from .hours import is_open_at
from .models import (
    DEFAULT_CONFIG,
    Line,
    PricedLine,
    PricedOption,
    PricedOrder,
    PricingConfig,
    Selection,
    Totals,
)

__all__ = [
    "DEFAULT_CONFIG",
    "InvalidSelection",
    "ItemUnavailable",
    "Line",
    "MenuVersionChanged",
    "PricedLine",
    "PricedOption",
    "PricedOrder",
    "PricingConfig",
    "PricingError",
    "RestaurantClosed",
    "Selection",
    "Totals",
    "is_open_at",
    "price_order",
]
