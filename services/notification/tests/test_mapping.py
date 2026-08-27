"""The event→notification policy matrix — every branch of the pure mapping."""

from notification.domain.mapping import order_drafts, payment_drafts


def _order_payload(**overrides):
    payload = {
        "order_id": "ord_1",
        "user_id": "usr_1",
        "restaurant_id": "rst_1",
        "restaurant_name": "Biryani House",
        "status": "CONFIRMED",
        "items": [{"menu_item_id": "itm_1", "qty": 2}],
        "totals": {"total_cents": 3247, "currency": "USD"},
        "delivery_address": {"line1": "12 Main St"},
        "cancel_reason": None,
    }
    payload.update(overrides)
    return payload


def test_confirmed_notifies_kitchen_and_customer():
    drafts = order_drafts("OrderConfirmed", _order_payload())
    assert [(d.recipient_type, d.recipient_id) for d in drafts] == [
        ("restaurant", "rst_1"),
        ("customer", "usr_1"),
    ]
    kitchen, customer = drafts
    assert kitchen.title == "New order to accept"
    assert "1 item, $32.47" in kitchen.body  # count + money, formatted
    assert "Biryani House" in customer.body


def test_confirmed_pluralizes_items():
    payload = _order_payload(items=[{"menu_item_id": "itm_1"}, {"menu_item_id": "itm_2"}])
    kitchen = order_drafts("OrderConfirmed", payload)[0]
    assert "2 items" in kitchen.body


def test_non_usd_money_uses_the_currency_code():
    payload = _order_payload(totals={"total_cents": 500, "currency": "PKR"})
    kitchen = order_drafts("OrderConfirmed", payload)[0]
    assert "5.00 PKR" in kitchen.body


def test_customer_cancel_tells_both_sides():
    drafts = order_drafts("OrderCancelled", _order_payload(cancel_reason="customer_cancelled"))
    assert [d.recipient_type for d in drafts] == ["customer", "restaurant"]
    assert "as you asked" in drafts[0].body
    assert drafts[1].title == "Order cancelled by the customer"


def test_platform_cancels_tell_only_the_customer():
    """The kitchen never saw a pre-confirmation order — telling it about
    the cancel would be noise about mail it never received."""
    for reason, phrase in [
        ("restaurant_rejected", "couldn't take your order"),
        ("restaurant_timeout", "didn't respond in time"),
        ("payment_declined", "card was declined"),
        ("item_unavailable", "sold out"),
        ("at_capacity", "can't take more orders"),
    ]:
        drafts = order_drafts("OrderCancelled", _order_payload(cancel_reason=reason))
        assert [d.recipient_type for d in drafts] == ["customer"], reason
        assert phrase in drafts[0].body, reason


def test_unknown_cancel_reason_falls_back_gracefully():
    drafts = order_drafts("OrderCancelled", _order_payload(cancel_reason="solar_flare"))
    assert drafts[0].body == "Your order at Biryani House was cancelled."
    drafts = order_drafts("OrderCancelled", _order_payload(cancel_reason=None))
    assert len(drafts) == 1  # None reason → customer-only, fallback copy


def test_delivered_notifies_the_customer():
    (draft,) = order_drafts("OrderDelivered", _order_payload(status="DELIVERED"))
    assert (draft.recipient_type, draft.kind) == ("customer", "order_delivered")
    assert "Biryani House" in draft.body


def test_deliberate_order_silences():
    """Placed (the customer just did it; the kitchen must not pre-heat
    unpaid orders) and Settled (bookkeeping) mint nothing."""
    assert order_drafts("OrderPlaced", _order_payload(status="PLACED")) == []
    assert order_drafts("OrderSettled", _order_payload(status="SETTLED")) == []


def test_refund_notifies_the_customer_with_the_amount():
    payload = {"order_id": "ord_1", "status": "REFUNDED", "amount_cents": 3247, "currency": "USD"}
    (draft,) = payment_drafts("RefundProcessed", payload, user_id="usr_1")
    assert (draft.recipient_type, draft.recipient_id) == ("customer", "usr_1")
    assert "$32.47" in draft.body


def test_deliberate_payment_silences():
    payload = {"order_id": "ord_1", "status": "AUTHORIZED", "amount_cents": 1, "currency": "USD"}
    assert payment_drafts("PaymentAuthorized", payload, user_id="u") == []
    assert payment_drafts("PaymentCaptured", payload, user_id="u") == []


def test_no_rider_cancel_tells_both_sides():
    """FR-32's cancel is the one born AFTER the kitchen cooked — the
    restaurant hears why, not just that the order vanished."""
    drafts = order_drafts(
        "OrderCancelled",
        {
            "order_id": "ord_1",
            "user_id": "usr_1",
            "restaurant_id": "rst_1",
            "restaurant_name": "Biryani House",
            "cancel_reason": "no_rider_available",
        },
    )
    assert [(d.recipient_type, d.kind) for d in drafts] == [
        ("customer", "order_cancelled"),
        ("restaurant", "order_cancelled"),
    ]
    assert "couldn't find a rider" in drafts[0].body
    assert "was not charged" in drafts[0].body
    assert drafts[1].title == "No rider available"


def test_brand_id_addresses_the_kitchen_drafts(  # ADR-0028: one bell per brand
):
    confirmed = order_drafts("OrderConfirmed", _order_payload(brand_id="brd_1"))
    assert (confirmed[0].recipient_type, confirmed[0].recipient_id) == ("restaurant", "brd_1")
    assert (confirmed[1].recipient_type, confirmed[1].recipient_id) == ("customer", "usr_1")

    cancelled = order_drafts(
        "OrderCancelled",
        _order_payload(status="CANCELLED", cancel_reason="customer_cancelled", brand_id="brd_1"),
    )
    assert ("restaurant", "brd_1") in [(d.recipient_type, d.recipient_id) for d in cancelled]


def test_null_brand_id_falls_back_to_the_branch():
    """Transitional payloads carry brand_id=None — the pre-brands address."""
    drafts = order_drafts("OrderConfirmed", _order_payload(brand_id=None))
    assert (drafts[0].recipient_type, drafts[0].recipient_id) == ("restaurant", "rst_1")
