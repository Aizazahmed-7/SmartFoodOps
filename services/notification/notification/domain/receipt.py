"""Receipt content — pure functions from a claim-check row to a document.

Split on purpose, because a PDF is a binary format and binary output is
where test coverage goes to die:

- `receipt_lines()` renders the DOCUMENT as plain text — every layout and
  copy decision (item columns, money formatting, when a discount line
  appears) is a string the tests can read.
- `receipt_pdf()` wraps those exact lines in a PDF envelope (fpdf2) — the
  tests assert the envelope (magic bytes, non-trivial size), never pixels.

Nothing here does I/O and nothing reads a clock: the settled instant comes
from the event, the same discipline as hours.py.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .mapping import money


@dataclass(frozen=True)
class ReceiptData:
    """The claim-check row, as the renderer sees it — copied verbatim from
    the OrderSettled payload at consume time (items/totals keep the event's
    wire shape, so this module and the event contract can never drift
    apart without a test noticing)."""

    order_id: str
    user_id: str
    restaurant_name: str
    items: list[dict[str, Any]]
    totals: dict[str, Any]
    settled_at: datetime


def receipt_subject(data: ReceiptData) -> str:
    return f"Your {data.restaurant_name} receipt"


def receipt_body(data: ReceiptData) -> str:
    total = money(int(data.totals["total_cents"]), str(data.totals["currency"]))
    return (
        f"Your order at {data.restaurant_name} is settled — {total}, paid in full.\n"
        f"The receipt is attached as a PDF. Order reference: {data.order_id}."
    )


_WIDTH = 46  # money right-aligns to this column in the text layout


def _row(label: str, amount: str) -> str:
    return f"{label}{amount:>{_WIDTH - len(label)}}"


def receipt_lines(data: ReceiptData) -> list[str]:
    """The whole document, one string per printed line."""
    currency = str(data.totals["currency"])
    settled = data.settled_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "SmartFoodOps — payment receipt",
        f"Order {data.order_id}",
        f"Settled {settled}",
        f"Restaurant: {data.restaurant_name}",
        "-" * _WIDTH,
    ]
    for item in data.items:
        label = f"{item['qty']} x {item['name']}"
        lines.append(_row(label, money(int(item["line_total_cents"]), currency)))
    lines.append("-" * _WIDTH)
    lines.append(_row("Subtotal", money(int(data.totals["subtotal_cents"]), currency)))
    if int(data.totals["discount_cents"]) > 0:
        lines.append(_row("Discount", "-" + money(int(data.totals["discount_cents"]), currency)))
    lines.append(_row("Delivery fee", money(int(data.totals["fee_cents"]), currency)))
    lines.append(_row("Tax", money(int(data.totals["tax_cents"]), currency)))
    lines.append(_row("Total", money(int(data.totals["total_cents"]), currency)))
    lines.append("")
    lines.append("Paid in full. Thank you for ordering with SmartFoodOps.")
    return lines


def receipt_pdf(data: ReceiptData) -> bytes:
    """The lines, in a Courier-set PDF (monospace is what makes the text
    layout's column arithmetic hold on paper too).

    The built-in fonts speak cp1252, not Unicode, and fpdf2 RAISES on a
    character outside it — which would turn one restaurant named in
    Vietnamese into a receipt that never renders, retries forever, and
    pages someone. So every line is degraded to cp1252 with replacement:
    a `?` in a name is a cosmetic loss, a crashed render is a lost
    receipt. Loss degrades, never corrupts."""
    pdf = FPDF()
    pdf.core_fonts_encoding = "cp1252"  # latin-1 default lacks even the em-dash
    pdf.add_page()
    pdf.set_font("Courier", size=10)
    for line in receipt_lines(data):
        printable = line.encode("cp1252", "replace").decode("cp1252")
        pdf.cell(0, 5, printable, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())
