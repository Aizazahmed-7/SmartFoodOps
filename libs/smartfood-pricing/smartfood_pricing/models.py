"""Pricing value objects — frozen, extra-forbidding, integer-cents only.

The library depends on pydantic and NOTHING else: the same `price_order`
call serves the stateless /v1/quote and the placement transaction's money
snapshot (ADR-0015), so it must be importable anywhere — including, later,
a Temporal activity.
"""

from pydantic import BaseModel, ConfigDict, Field


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Selection(_Value):
    group_id: str = Field(min_length=1, max_length=64)
    option_id: str = Field(min_length=1, max_length=64)


class Line(_Value):
    item_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=50)
    options: tuple[Selection, ...] = Field(default=(), max_length=10)


class PricingConfig(_Value):
    """The money knobs. Flat delivery fee + basis-point tax (8.25% = 825 bp)
    — deliberately simple for Part A; promos/discounts are FR-10 (P1)."""

    delivery_fee_cents: int = Field(default=199, ge=0, le=10_000)
    tax_basis_points: int = Field(default=825, ge=0, le=3_000)


DEFAULT_CONFIG = PricingConfig()


class PricedOption(_Value):
    group_id: str
    group_name: str
    option_id: str
    name: str
    price_delta_cents: int


class PricedLine(_Value):
    item_id: str
    name: str
    qty: int
    unit_price_cents: int  # base item price, deltas listed separately
    options: tuple[PricedOption, ...]
    line_total_cents: int  # (unit + Σ deltas) * qty


class Totals(_Value):
    subtotal_cents: int
    discount_cents: int  # always 0 in Part A — the FR-16 snapshot shape
    fee_cents: int
    tax_cents: int
    total_cents: int


class PricedOrder(_Value):
    lines: tuple[PricedLine, ...]
    totals: Totals
    currency: str
    menu_version: int
    restaurant_name: str
