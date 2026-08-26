"""Receipt content — the text layout is the tested truth; the PDF is an
envelope asserted by magic bytes (the receipt.py split exists for this)."""

from datetime import UTC, datetime

from notification.domain.receipt import (
    ReceiptData,
    receipt_body,
    receipt_lines,
    receipt_pdf,
    receipt_subject,
)

SETTLED = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)


def _data(**overrides) -> ReceiptData:
    fields = {
        "order_id": "ord_abc123",
        "user_id": "usr_1",
        "restaurant_name": "Biryani House",
        "items": [
            {
                "name": "Chicken Biryani",
                "qty": 2,
                "unit_price_cents": 1299,
                "line_total_cents": 2598,
            },
            {"name": "Raita", "qty": 1, "unit_price_cents": 349, "line_total_cents": 349},
        ],
        "totals": {
            "subtotal_cents": 2947,
            "discount_cents": 0,
            "fee_cents": 299,
            "tax_cents": 235,
            "total_cents": 3481,
            "currency": "USD",
        },
        "settled_at": SETTLED,
    }
    fields.update(overrides)
    return ReceiptData(**fields)


def test_subject_and_body_carry_the_essentials():
    data = _data()
    assert receipt_subject(data) == "Your Biryani House receipt"
    body = receipt_body(data)
    assert "$34.81" in body and "paid in full" in body
    assert "ord_abc123" in body  # the reference the customer can quote at support


def test_lines_render_items_and_totals_right_aligned():
    lines = receipt_lines(_data())
    assert lines[0] == "SmartFoodOps — payment receipt"
    assert "Settled 2026-08-25 01:30 UTC" in lines
    item_line = next(line for line in lines if "Chicken Biryani" in line)
    assert item_line.startswith("2 x Chicken Biryani")
    assert item_line.endswith("$25.98") and len(item_line) == 46  # the money column
    assert any(line.startswith("Total") and line.endswith("$34.81") for line in lines)
    # No discount was granted, so no discount line appears.
    assert not any("Discount" in line for line in lines)


def test_discount_line_appears_only_when_granted():
    totals = dict(_data().totals, discount_cents=500)
    lines = receipt_lines(_data(totals=totals))
    assert any(line.startswith("Discount") and line.endswith("-$5.00") for line in lines)


def test_pdf_is_a_pdf_with_substance():
    document = receipt_pdf(_data())
    assert document.startswith(b"%PDF")
    assert len(document) > 500  # an empty envelope is ~300 bytes


def test_pdf_degrades_unencodable_glyphs_instead_of_crashing():
    """The core fonts speak cp1252; a Vietnamese restaurant name must cost
    a replacement glyph, never the receipt itself."""
    document = receipt_pdf(_data(restaurant_name="Phở Hà Nội"))
    assert document.startswith(b"%PDF")
