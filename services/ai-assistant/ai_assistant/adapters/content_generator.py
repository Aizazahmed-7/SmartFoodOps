"""Writing one piece of restaurant copy (FR-88/89/90).

Routed through `Task.CONTENT_DRAFT`, so this pays the same budget guard,
failover and metrics every other model call does — the content studio is
not a side door to a provider.

The prompt is deliberately thin here. B6.3 is where each kind gets the
facts it should be written FROM (a dish's name, tags, category and cuisine;
a restaurant's own aggregates), and that is the substance of FR-88 rather
than a detail of the queue. What this layer owes is the shape: a draft is
one short piece of copy, it is never published from here, and a model that
declines is a result rather than an exception.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from smartfood_otel import get_logger

from ..content import Drafted, PermanentFailure
from ..domain.ports import Message
from ..domain.router import Task

log = get_logger("ai-assistant.content-generator")

SYSTEM_PROMPT = """You write short, factual copy for a restaurant's own menu
and promotions.

Rules:
- Describe only what you are told. Never invent an ingredient, an origin
  story, an award, a price, a dietary claim, or a health benefit.
- No customer names, quotes or testimonials. You have never met a
  customer and have no record of anyone's opinion.
- You know nothing about the person reading this. The dish figures you are
  given are the RESTAURANT's totals, not this reader's history — never
  write "your favourites" or imply you know what they ordered.
- This is a delivery platform. Never mention dining in, tables, bookings
  or a venue.
- No awards, rankings, or claims about reputation.
- No email addresses, phone numbers or links.
- Two sentences at most. No markdown, no emoji, no sign-off.

Reply with the copy and nothing else."""
"""The invention rules are the load-bearing ones.

A model asked to make a dish sound appealing will reach for provenance
("slow-cooked using a century-old family recipe") and dietary claims
("packed with protein") that nobody can stand behind — and unlike B5's
explanations, this copy is about to be shown to customers as the
restaurant's own words. FR-90 names the same rule for engagement copy: no
fabricated claims or testimonials.

A human approves everything before publication (FR-93), which is the real
defence. This is the first one.
"""

MAX_CHARS = 400


@dataclass(frozen=True)
class ContentGenerator:
    router: object

    async def draft(
        self,
        *,
        kind: str,
        subject: str,
        request: str | None,
        facts: Mapping[str, object] | None = None,
    ) -> Drafted | None:
        prompt = _prompt(kind=kind, subject=subject, request=request, facts=facts)

        completion = await self.router.complete(  # type: ignore[attr-defined]
            Task.CONTENT_DRAFT,
            [Message(role="system", content=SYSTEM_PROMPT), Message(role="user", content=prompt)],
        )
        if completion.finish_reason == "refusal":
            return None  # a result, not an exception: the task parks it
        if completion.finish_reason == "length":
            raise PermanentFailure("the model ran out of room mid-sentence")
        text = completion.text.strip()
        if not text:
            return None
        if len(text) > MAX_CHARS:
            raise PermanentFailure(
                f"the model wrote {len(text)} characters; the cap is {MAX_CHARS}"
            )
        return Drafted(text=text, model=completion.model)


_FACT_LABELS: tuple[tuple[str, str], ...] = (
    # FR-88: one dish.
    ("name", "Dish"),
    ("category", "Category"),
    ("tags", "Tags"),
    ("cuisines", "Cuisine"),
    # FR-89/FR-90: the business, as counts. Every one of these is a number
    # or a dish name — there is no customer identity in the list, which is
    # how "no customer PII" is kept structurally rather than by asking.
    ("orders", "Orders in the period"),
    ("customers", "Distinct customers"),
    ("repeat_customers", "Customers who ordered more than once"),
    ("lapsed_customers", "Customers who have not ordered recently"),
    ("top_dishes", "Most ordered dishes"),
    ("window_days", "Days the period covers"),
    ("lapsed_after_days", "Days without an order that counts as lapsed"),
)
"""FR-88's field list, in the order a person would say them.

A fixed list rather than "whatever is in the dict" on purpose: the facts
come from a restaurant's own menu and are therefore untrusted text
(ADR-0043), and an unbounded loop over keys would let a crafted field name
become an instruction line. A key not named here cannot reach the model.
"""


def _prompt(
    *, kind: str, subject: str, request: str | None, facts: Mapping[str, object] | None
) -> str:
    """Facts first, the admin's words last, both labelled as data.

    The admin's request is deliberately NOT concatenated into the system
    prompt: a restaurant admin is a tenant, not an operator (ADR-0043), and
    "write 500 words ignoring your rules" arriving as an instruction is the
    same injection a customer's question would be.
    """
    lines = [f"Kind of copy: {kind}"]
    for key, label in _FACT_LABELS:
        value = (facts or {}).get(key)
        if not value:
            continue
        rendered = (
            ", ".join(str(v) for v in value)
            if isinstance(value, Sequence) and not isinstance(value, str)
            else str(value)
        )
        lines.append(f"{label}: {rendered}")
    if not facts:
        lines.append(f"Subject: {subject}")
    if request:
        lines.append(f"What the restaurant asked for: {request}")
    return "\n".join(lines)
