"""The pricing engine — deterministic integer math over a catalog snapshot.

Input contract: `snapshot` is catalog's internal pricing read VERBATIM
(GET /v1/internal/restaurants/{rid}/snapshot — restaurant + items in request
order + missing_item_ids). The engine trusts its shape; it was produced by
a service we own.

Error policy: ALL lines are inspected before raising, so one response names
every unavailable item and every selection violation — a cart fixes itself
in one round-trip, not five. State problems (ItemUnavailable) outrank
client bugs (InvalidSelection): an unavailable item's selections can't be
validated anyway, and the client will re-render the menu regardless.

Stock is deliberately NOT a pricing concern: quotes must not read inventory
(that's ValidateAndReserve's job at placement, with a real reservation).
"""

from typing import Any

from .errors import InvalidSelection, ItemUnavailable, MenuVersionChanged, RestaurantClosed
from .models import (
    DEFAULT_CONFIG,
    Line,
    PricedLine,
    PricedOption,
    PricedOrder,
    PricingConfig,
    Totals,
)


def price_order(
    snapshot: dict[str, Any],
    lines: list[Line],
    *,
    expected_menu_version: int | None = None,
    config: PricingConfig = DEFAULT_CONFIG,
) -> PricedOrder:
    restaurant = snapshot["restaurant"]

    # Version drift first: if the caller pinned a version (placement does;
    # quote passes None and self-heals), a mismatch invalidates everything
    # else we might say — re-sync, then talk.
    if expected_menu_version is not None and expected_menu_version != restaurant["version"]:
        raise MenuVersionChanged(current=restaurant["version"])

    if restaurant["status"] != "open":
        raise RestaurantClosed(restaurant["id"])

    if not lines:
        raise InvalidSelection([{"field": "lines", "issue": "at least one line required"}])

    by_id = {item["id"]: item for item in snapshot["items"]}
    missing = set(snapshot.get("missing_item_ids", []))

    unavailable: set[str] = set()
    violations: list[dict[str, str]] = []
    priced: list[PricedLine] = []
    currency = "USD"

    for index, line in enumerate(lines):
        item = by_id.get(line.item_id)
        if item is None or line.item_id in missing or not item["available"]:
            unavailable.add(line.item_id)
            continue
        priced_line = _price_line(index, line, item, unavailable, violations)
        if priced_line is not None:
            priced.append(priced_line)
            currency = item["currency"]

    if unavailable:
        raise ItemUnavailable(sorted(unavailable))
    if violations:
        raise InvalidSelection(violations)

    subtotal = sum(line.line_total_cents for line in priced)
    discount = 0  # FR-16 shape; promos are FR-10 (P1)
    fee = config.delivery_fee_cents
    tax = subtotal * config.tax_basis_points // 10_000  # floor — stated policy
    totals = Totals(
        subtotal_cents=subtotal,
        discount_cents=discount,
        fee_cents=fee,
        tax_cents=tax,
        total_cents=subtotal - discount + fee + tax,
    )
    return PricedOrder(
        lines=tuple(priced),
        totals=totals,
        currency=currency,
        menu_version=restaurant["version"],
        restaurant_name=restaurant["name"],
    )


def _price_line(
    index: int,
    line: Line,
    item: dict[str, Any],
    unavailable: set[str],
    violations: list[dict[str, str]],
) -> PricedLine | None:
    """Price one line. Returns None when the line can't be priced — the
    reason has already been recorded in `unavailable` or `violations`."""
    groups = {group["id"]: group for group in item["modifier_groups"]}
    field = f"lines[{index}].options"

    chosen: list[PricedOption] = []
    picked_per_group: dict[str, set[str]] = {}
    for selection in line.options:
        group = groups.get(selection.group_id)
        if group is None:
            # The menu no longer has this group: structural drift, not a
            # client bug — the cart was built against an older menu.
            unavailable.add(line.item_id)
            return None
        option = next((o for o in group["options"] if o["id"] == selection.option_id), None)
        if option is None:
            unavailable.add(line.item_id)
            return None
        already = picked_per_group.setdefault(group["id"], set())
        if selection.option_id in already:
            violations.append(
                {"field": field, "issue": f"duplicate option in group {group['name']!r}"}
            )
            return None
        already.add(selection.option_id)
        chosen.append(
            PricedOption(
                group_id=group["id"],
                group_name=group["name"],
                option_id=option["id"],
                name=option["name"],
                price_delta_cents=option["price_delta_cents"],
            )
        )

    # min/max_select apply to EVERY group on the item, including ones the
    # client never mentioned (a required Size left unpicked is a violation).
    for group in item["modifier_groups"]:
        count = len(picked_per_group.get(group["id"], ()))
        if count < group["min_select"]:
            violations.append(
                {
                    "field": field,
                    "issue": f"group {group['name']!r} requires at least "
                    f"{group['min_select']} selection(s)",
                }
            )
            return None
        if count > group["max_select"]:
            violations.append(
                {
                    "field": field,
                    "issue": f"group {group['name']!r} allows at most "
                    f"{group['max_select']} selection(s)",
                }
            )
            return None

    unit_with_options = item["price_cents"] + sum(o.price_delta_cents for o in chosen)
    return PricedLine(
        item_id=line.item_id,
        name=item["name"],
        qty=line.qty,
        unit_price_cents=item["price_cents"],
        options=tuple(chosen),
        line_total_cents=unit_with_options * line.qty,
    )
