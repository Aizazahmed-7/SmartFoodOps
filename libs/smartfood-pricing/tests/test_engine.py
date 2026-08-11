"""Every branch of the pricing engine: math, drift, availability,
selection rules, config, and input bounds."""

import pytest
from pydantic import ValidationError
from smartfood_pricing import (
    InvalidSelection,
    ItemUnavailable,
    Line,
    MenuVersionChanged,
    PricingConfig,
    RestaurantClosed,
    Selection,
    price_order,
)


def snap(*, version=3, status="open", items=(), missing=()):
    return {
        "restaurant": {
            "id": "rst_1",
            "name": "Biryani House",
            "city": "springfield",
            "status": status,
            "version": version,
        },
        "items": list(items),
        "missing_item_ids": list(missing),
    }


def item(id="itm_a", price=1000, available=True, groups=(), currency="USD", name="Biryani"):
    return {
        "id": id,
        "name": name,
        "price_cents": price,
        "currency": currency,
        "available": available,
        "modifier_groups": list(groups),
    }


def group(id="grp_size", name="Size", min_select=1, max_select=1, options=()):
    return {
        "id": id,
        "name": name,
        "min_select": min_select,
        "max_select": max_select,
        "options": list(options),
    }


def option(id="opt_large", name="Large", delta=300):
    return {"id": id, "name": name, "price_delta_cents": delta}


SIZE = group(options=[option("opt_reg", "Regular", 0), option("opt_large", "Large", 300)])
ADDONS = group(
    id="grp_add",
    name="Add-ons",
    min_select=0,
    max_select=2,
    options=[option("opt_cheese", "Cheese", 150), option("opt_less", "Half Portion", -200)],
)


# ── math ───────────────────────────────────────────────────────────


def test_happy_single_line_no_options():
    priced = price_order(snap(items=[item()]), [Line(item_id="itm_a", qty=2)])
    line = priced.lines[0]
    assert (line.unit_price_cents, line.line_total_cents) == (1000, 2000)
    assert priced.totals.subtotal_cents == 2000
    assert priced.totals.fee_cents == 199
    assert priced.totals.tax_cents == 2000 * 825 // 10_000  # 165
    assert priced.totals.total_cents == 2000 + 199 + 165
    assert priced.totals.discount_cents == 0
    assert priced.menu_version == 3
    assert priced.restaurant_name == "Biryani House"
    assert priced.currency == "USD"


def test_option_deltas_including_negative():
    lines = [
        Line(
            item_id="itm_a",
            qty=3,
            options=(
                Selection(group_id="grp_size", option_id="opt_large"),
                Selection(group_id="grp_add", option_id="opt_less"),
            ),
        )
    ]
    priced = price_order(snap(items=[item(groups=[SIZE, ADDONS])]), lines)
    # (1000 + 300 - 200) * 3
    assert priced.lines[0].line_total_cents == 3300
    assert [o.name for o in priced.lines[0].options] == ["Large", "Half Portion"]


def test_multi_line_subtotal_and_qty_bounds():
    items = [item(), item(id="itm_b", price=50, name="Raita")]
    lines = [Line(item_id="itm_a", qty=1), Line(item_id="itm_b", qty=50)]
    priced = price_order(snap(items=items), lines)
    assert priced.totals.subtotal_cents == 1000 + 50 * 50


def test_tax_floors_never_rounds_up():
    priced = price_order(snap(items=[item(price=999)]), [Line(item_id="itm_a", qty=1)])
    assert priced.totals.tax_cents == 999 * 825 // 10_000  # 82.41… → 82


def test_zero_priced_item_totals_are_fee_only():
    priced = price_order(snap(items=[item(price=0)]), [Line(item_id="itm_a", qty=1)])
    assert priced.totals.subtotal_cents == 0
    assert priced.totals.tax_cents == 0
    assert priced.totals.total_cents == 199


def test_config_override():
    config = PricingConfig(delivery_fee_cents=0, tax_basis_points=0)
    priced = price_order(snap(items=[item()]), [Line(item_id="itm_a", qty=1)], config=config)
    assert priced.totals.total_cents == 1000


def test_currency_taken_from_items():
    priced = price_order(snap(items=[item(currency="PKR")]), [Line(item_id="itm_a", qty=1)])
    assert priced.currency == "PKR"


# ── drift & state ──────────────────────────────────────────────────


def test_version_pin_match_passes_and_mismatch_raises():
    snapshot = snap(version=7, items=[item()])
    assert price_order(snapshot, [Line(item_id="itm_a", qty=1)], expected_menu_version=7)
    with pytest.raises(MenuVersionChanged) as exc:
        price_order(snapshot, [Line(item_id="itm_a", qty=1)], expected_menu_version=6)
    assert exc.value.current == 7


def test_no_pin_means_self_healing():
    priced = price_order(snap(version=99, items=[item()]), [Line(item_id="itm_a", qty=1)])
    assert priced.menu_version == 99  # caller learns the current version


def test_paused_restaurant_is_closed():
    with pytest.raises(RestaurantClosed):
        price_order(snap(status="paused", items=[item()]), [Line(item_id="itm_a", qty=1)])


def test_version_drift_outranks_closed():
    with pytest.raises(MenuVersionChanged):
        price_order(
            snap(status="paused", version=5),
            [Line(item_id="itm_a", qty=1)],
            expected_menu_version=4,
        )


# ── availability ───────────────────────────────────────────────────


def test_missing_unknown_and_86d_items_all_reported_sorted():
    snapshot = snap(items=[item(), item(id="itm_dead", available=False)], missing=["itm_z"])
    lines = [
        Line(item_id="itm_z", qty=1),  # catalog says: not this restaurant's
        Line(item_id="itm_ghost", qty=1),  # not in the response at all
        Line(item_id="itm_dead", qty=1),  # 86'd
        Line(item_id="itm_a", qty=1),  # fine — but the order still fails
    ]
    with pytest.raises(ItemUnavailable) as exc:
        price_order(snapshot, lines)
    assert exc.value.item_ids == ["itm_dead", "itm_ghost", "itm_z"]


def test_unknown_group_or_option_is_structural_drift():
    snapshot = snap(items=[item(groups=[SIZE])])
    with pytest.raises(ItemUnavailable):
        price_order(
            snapshot,
            [
                Line(
                    item_id="itm_a", qty=1, options=(Selection(group_id="grp_gone", option_id="x"),)
                )
            ],
        )
    with pytest.raises(ItemUnavailable):
        price_order(
            snapshot,
            [
                Line(
                    item_id="itm_a",
                    qty=1,
                    options=(Selection(group_id="grp_size", option_id="opt_gone"),),
                )
            ],
        )


def test_unavailability_outranks_selection_violations():
    snapshot = snap(items=[item(groups=[SIZE]), item(id="itm_b", available=False, name="Dead")])
    lines = [
        Line(item_id="itm_a", qty=1),  # violates min_select on Size
        Line(item_id="itm_b", qty=1),  # unavailable
    ]
    with pytest.raises(ItemUnavailable):
        price_order(snapshot, lines)


# ── selection rules ────────────────────────────────────────────────


def test_required_group_unpicked_is_violation():
    with pytest.raises(InvalidSelection) as exc:
        price_order(snap(items=[item(groups=[SIZE])]), [Line(item_id="itm_a", qty=1)])
    assert "at least 1" in exc.value.details[0]["issue"]


def test_max_select_exceeded_is_violation():
    lines = [
        Line(
            item_id="itm_a",
            qty=1,
            options=(
                Selection(group_id="grp_size", option_id="opt_reg"),
                Selection(group_id="grp_size", option_id="opt_large"),
            ),
        )
    ]
    with pytest.raises(InvalidSelection) as exc:
        price_order(snap(items=[item(groups=[SIZE])]), lines)
    assert "at most 1" in exc.value.details[0]["issue"]


def test_duplicate_option_is_violation():
    lines = [
        Line(
            item_id="itm_a",
            qty=1,
            options=(
                Selection(group_id="grp_add", option_id="opt_cheese"),
                Selection(group_id="grp_add", option_id="opt_cheese"),
            ),
        )
    ]
    with pytest.raises(InvalidSelection) as exc:
        price_order(snap(items=[item(groups=[ADDONS])]), lines)
    assert "duplicate" in exc.value.details[0]["issue"]


def test_violations_reported_across_all_lines():
    snapshot = snap(items=[item(groups=[SIZE]), item(id="itm_b", groups=[SIZE], name="Karahi")])
    lines = [Line(item_id="itm_a", qty=1), Line(item_id="itm_b", qty=1)]
    with pytest.raises(InvalidSelection) as exc:
        price_order(snapshot, lines)
    assert [d["field"] for d in exc.value.details] == ["lines[0].options", "lines[1].options"]


def test_optional_group_may_be_skipped():
    priced = price_order(snap(items=[item(groups=[ADDONS])]), [Line(item_id="itm_a", qty=1)])
    assert priced.lines[0].line_total_cents == 1000


def test_empty_lines_rejected():
    with pytest.raises(InvalidSelection):
        price_order(snap(items=[item()]), [])


# ── input bounds (the Line model is its own bouncer) ───────────────


def test_line_model_bounds():
    with pytest.raises(ValidationError):
        Line(item_id="itm_a", qty=0)
    with pytest.raises(ValidationError):
        Line(item_id="itm_a", qty=51)
    with pytest.raises(ValidationError):
        Line(item_id="", qty=1)
    with pytest.raises(ValidationError):
        Line(
            item_id="itm_a",
            qty=1,
            options=tuple(Selection(group_id=f"g{i}", option_id=f"o{i}") for i in range(11)),
        )
    with pytest.raises(ValidationError):
        Line(item_id="itm_a", qty=1, unknown_field=1)  # type: ignore[call-arg]
