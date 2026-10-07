"""Explanations, written from templates (FR-87).

The floor of the explanation engine. No model is reached from this module —
not "no model by default", none at all — which is what makes FR-87's
acceptance criterion structural rather than a promise: with `llm_api_key=""`
no provider is even constructed (`main.py`), and this path never wanted one.
A model may later rewrite these sentences to read better; it can never be
what makes an answer exist.

The key is `(reason_code, locale, bucket)`, and each part earns its place:

- **reason_code** is the resolver's verdict. Copy is written per cause
  because "we're finding you a courier" and "the kitchen is at capacity"
  are not the same sentence with different nouns.
- **locale** is here from the start rather than retrofitted. Only `en`
  ships, and the lookup falls back to it, so adding `ur` is a dictionary
  entry and not a refactor.
- **bucket** is how long the stage has been running. The same cause reads
  differently at 20 seconds and at 20 minutes: "just sent to the
  restaurant" and "the restaurant has had this for 20 minutes" are both
  AWAITING_RESTAURANT, and only one of them is honest at each moment.

Every sentence states facts the resolver supplied and nothing else. There
are no apologies that imply a fault nobody has established, and no promises
about when food will arrive — the resolver bounds stages, not deliveries.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .explain import ReasonCode, Verdict

BASE_LOCALE = "en"


class Bucket(StrEnum):
    """How long the current stage has been running.

    OVERDUE is not a duration and outranks every duration: once a real
    Temporal deadline has passed, that is the salient fact whatever the
    clock says. It can only be reached where a budget exists, so an
    unbudgeted stage (the kitchen, the road) never lands here — it cannot
    be past a deadline it does not have.

    UNKNOWN is the honest bucket for a stage with no start moment, which is
    what a pre-ADR-0046 order looks like. Its copy carries no duration,
    because there is none to carry.
    """

    JUST_NOW = "just_now"
    SHORT = "short"
    EXTENDED = "extended"
    LONG = "long"
    OVERDUE = "overdue"
    UNKNOWN = "unknown"


SHORT_AFTER_S = 60
EXTENDED_AFTER_S = 5 * 60
LONG_AFTER_S = 15 * 60


def bucket_for(verdict: Verdict) -> Bucket:
    if verdict.overdue:
        return Bucket.OVERDUE
    elapsed = verdict.stage_elapsed_s
    if elapsed is None:
        return Bucket.UNKNOWN
    if elapsed < SHORT_AFTER_S:
        return Bucket.JUST_NOW
    if elapsed < EXTENDED_AFTER_S:
        return Bucket.SHORT
    if elapsed < LONG_AFTER_S:
        return Bucket.EXTENDED
    return Bucket.LONG


@dataclass(frozen=True)
class Explanation:
    text: str
    reason: ReasonCode
    bucket: Bucket
    locale: str
    source: str = "template"
    """Which layer produced the words: "template", or "fallback" when the
    chosen copy could not be filled in.

    A fallback renders the SAME sentence a genuine UNKNOWN does, because a
    customer should not get stranger wording on account of our bug. That
    makes the text useless for telling the two apart, which is exactly why
    this field exists: `source == "fallback"` is a defect to fix, while an
    UNKNOWN verdict rendered from its own template is the system working.
    A caller that cannot distinguish them will never learn it has a broken
    template. (It also carries "model" once a rewrite layer lands, so a
    reader of a stored answer can tell whether a machine touched it.)
    """


_HANDOFF = (
    "We can't tell you exactly what's happening with this order right now. "
    "Support can look into it for you."
)
"""The one sentence for "we cannot say", used by both honest ignorance and
internal failure. Kept identical on purpose — see `Explanation.source`."""


_HOLD_RELEASED = "Your card hold has been released."
_HOLD_RELEASING = "Your card hold is being released."
_NEVER_CHARGED = "You won't be charged."
_MONEY_UNKNOWN = ""
"""The money sentence, chosen by facts rather than written per template.

Four cases, not two, and the fourth is the important one:

- a hold existed and the unwind has finished — released, past tense;
- a hold existed and the unwind is still running (CANCELLING, or an
  overdue stage predicting a cancellation) — releasing, present tense,
  because the money is still on the card while compensations retry;
- no hold ever existed — nothing will be charged;
- **nobody recorded it** — say nothing. Rows placed before the milestone
  columns existed carry no evidence either way, and the worst sentence
  this engine can produce is telling someone they were not charged while a
  hold sits on their card. An absent clause is a smaller failure than a
  confident wrong one.

Templates end with `{money}` so the sentence cannot drift between them.
"""


def money_sentence(held: object, unwind_done: object) -> str:
    if held is None:
        return _MONEY_UNKNOWN
    if not held:
        return _NEVER_CHARGED
    return _HOLD_RELEASED if unwind_done else _HOLD_RELEASING


# ── the copy ───────────────────────────────────────────────────────
#
# A reason maps to `{bucket: text}`, and `None` is that reason's default
# for every bucket it does not spell out. Terminal causes use only the
# default: a cancelled order does not become a different cancellation
# because a customer asked about it twenty minutes later.
#
# Placeholders are drawn from the verdict and nothing else — `{elapsed}`
# (a formatted duration, so plural rules live in one place rather than in
# fifteen sentences) and `{minutes}` from `stage_elapsed_s`,
# `{active}`/`{capacity}` from a kitchen-load fact.
# A template naming anything else is a bug, and `test_render.py` renders
# every one of these against a real verdict to prove none does.

_EN: Mapping[ReasonCode, Mapping[Bucket | None, str]] = {
    ReasonCode.PAYMENT_IN_PROGRESS: {
        None: "We're confirming your payment and reserving your items.",
        Bucket.OVERDUE: (
            "Your order is taking longer than usual to confirm. If it doesn't clear "
            "shortly it will be cancelled automatically. {money}"
        ),
    },
    ReasonCode.AWAITING_RESTAURANT: {
        None: "Your order has been sent to the restaurant and is waiting to be accepted.",
        Bucket.JUST_NOW: "Your order has just been sent to the restaurant.",
        Bucket.SHORT: (
            "The restaurant has had your order for {elapsed} and hasn't accepted it yet."
        ),
        Bucket.OVERDUE: (
            "The restaurant hasn't accepted your order within its decision window. It "
            "will be cancelled automatically. {money}"
        ),
    },
    ReasonCode.KITCHEN_NOT_STARTED: {
        None: "The restaurant has accepted your order and hasn't started cooking yet.",
        Bucket.SHORT: (
            "The restaurant accepted your order {elapsed} ago and hasn't started cooking yet."
        ),
        Bucket.EXTENDED: (
            "The restaurant accepted your order {elapsed} ago and hasn't started cooking yet."
        ),
        Bucket.LONG: (
            "The restaurant accepted your order {elapsed} ago and still hasn't "
            "started cooking. If you'd like to cancel, you can do that from this order."
        ),
    },
    ReasonCode.KITCHEN_PREPARING: {
        None: "The kitchen is preparing your order.",
        Bucket.SHORT: "The kitchen has been preparing your order for {elapsed}.",
        Bucket.EXTENDED: "The kitchen has been preparing your order for {elapsed}.",
        Bucket.LONG: (
            "The kitchen has been preparing your order for {elapsed}. We don't "
            "have an estimate for when it will be ready."
        ),
    },
    # No "{active} of {capacity}". `active > capacity` is a legal state
    # (capacity lowered under a running kitchen, ports.py), and it rendered
    # as "9 of the 8 orders it can take at once" — which reads as a broken
    # system rather than a busy one.
    ReasonCode.KITCHEN_BUSY: {
        None: (
            "The kitchen is at capacity — it's working through {active} orders at "
            "once, so yours is taking longer than usual."
        ),
        Bucket.LONG: (
            "The kitchen is at capacity — {active} orders at once — and has had yours "
            "for {elapsed}. We don't have an estimate for when it will be ready."
        ),
    },
    ReasonCode.AWAITING_COURIER: {
        None: "Your food is ready and we're finding a courier to collect it.",
        Bucket.SHORT: (
            "Your food has been ready for {elapsed} and we're still finding a "
            "courier to collect it."
        ),
        Bucket.EXTENDED: (
            "Your food has been ready for {elapsed} and we're still finding a "
            "courier to collect it."
        ),
        Bucket.OVERDUE: (
            "Your food is ready but we couldn't find a courier in time. The order will "
            "be cancelled. {money}"
        ),
    },
    ReasonCode.COURIER_ON_THE_WAY: {
        None: "A courier is on the way to collect your order.",
        Bucket.SHORT: "A courier has been on the way to collect your order for {elapsed}.",
        # No "we're looking for another one": after the no-rider deadline
        # the cascade has stopped and the order is being cancelled, so that
        # promise can be false at the exact moment it is read.
        Bucket.OVERDUE: "The courier assigned to your order hasn't collected it yet.",
    },
    ReasonCode.IN_TRANSIT: {
        None: "Your order is on its way to you.",
        Bucket.SHORT: "Your order has been on its way to you for {elapsed}.",
        Bucket.EXTENDED: "Your order has been on its way to you for {elapsed}.",
        Bucket.LONG: (
            "Your order has been with the courier for {elapsed}. If something "
            "looks wrong, support can reach them directly."
        ),
    },
    ReasonCode.DELIVERED: {None: "Your order was delivered."},
    ReasonCode.REFUNDED: {None: "Your order was refunded."},
    ReasonCode.CANCELLED_BY_CUSTOMER: {None: "You cancelled this order. {money}"},
    ReasonCode.CANCELLED_BY_RESTAURANT: {
        None: "The restaurant couldn't take this order, so it was cancelled. {money}"
    },
    ReasonCode.CANCELLED_RESTAURANT_SILENT: {
        None: (
            "The restaurant didn't respond in time, so the order was cancelled "
            "automatically. {money}"
        )
    },
    ReasonCode.CANCELLED_ITEM_UNAVAILABLE: {
        None: ("Something in your order had just run out, so the order was cancelled. {money}")
    },
    ReasonCode.CANCELLED_KITCHEN_FULL: {
        None: (
            "The kitchen was at capacity and couldn't take another order, so this one "
            "was cancelled. {money}"
        )
    },
    ReasonCode.CANCELLED_PAYMENT_DECLINED: {
        None: (
            "Your payment was declined, so the order was cancelled. {money} "
            "You can try again with a different card."
        )
    },
    # The one cancellation born AFTER the food was cooked (FR-32). The copy
    # says so, because "we couldn't find a courier" reads very differently
    # when the customer knows their meal was made and then binned.
    ReasonCode.CANCELLED_NO_COURIER: {
        None: (
            "Your food was ready but we couldn't find a courier in time, so the order "
            "was cancelled. {money}"
        )
    },
    ReasonCode.CANCELLED_SYSTEM: {
        None: ("We couldn't complete this order and cancelled it. {money} Please try again.")
    },
    # UC-22's hand-off. It claims nothing, because the resolver knew nothing,
    # and it routes the customer somewhere a person can actually look.
    ReasonCode.UNKNOWN: {None: _HANDOFF},
}

_TEMPLATES: Mapping[str, Mapping[ReasonCode, Mapping[Bucket | None, str]]] = {BASE_LOCALE: _EN}


def supported(locale: str) -> str:
    """The locale we will actually answer in — never the one asked for.

    `TemplateCache.get` already falls back to English for anything it does
    not know, so an unsupported locale was silently answered in English
    while still being carried around as itself. That was harmless until it
    became a CACHE KEY: `locale` is a customer-controlled query parameter,
    so every distinct string was a fresh key and, with the rewrite layer, a
    fresh model call — unbounded, for copy that was English either way.

    Resolving here rather than at the edge keeps the two halves honest:
    what is rendered and what is cached are keyed by the same string.
    `en-GB` collapses to `en` for the same reason — two customers in the
    same situation should not read different sentences because of a
    browser header.
    """
    base = locale.strip().lower().replace("_", "-").split("-")[0]
    return base if base in _TEMPLATES else BASE_LOCALE


class TemplateCache:
    """The `(reason_code, locale, bucket)` lookup, with its fallbacks.

    Called a cache because that is FR-87's word and because it is the key a
    model-polished variant will be stored under when one exists. Today it
    holds static copy and computes nothing, so there is no eviction and no
    staleness to reason about — worth saying plainly rather than describing
    a dictionary as infrastructure.

    Lookup widens in one direction only: exact bucket, then the reason's
    default for that locale, then the same two in `en`. A locale that
    translates some buckets and not others therefore degrades to English
    for the rest instead of to nothing.
    """

    def __init__(
        self,
        templates: Mapping[str, Mapping[ReasonCode, Mapping[Bucket | None, str]]] | None = None,
    ) -> None:
        self._templates = templates if templates is not None else _TEMPLATES

    def get(self, reason: ReasonCode, locale: str, bucket: Bucket) -> str | None:
        for candidate in (locale, BASE_LOCALE):
            by_reason = self._templates.get(candidate)
            if by_reason is None:
                continue
            by_bucket = by_reason.get(reason)
            if by_bucket is None:
                continue
            text = by_bucket.get(bucket) or by_bucket.get(None)
            if text is not None:
                return text
        # No copy for this reason at all. Reachable only if the resolver
        # gains a code the registry has not caught up with, which
        # `test_every_reason_code_has_copy` exists to prevent.
        return None


def context_for(verdict: Verdict) -> dict[str, object]:
    """The closed vocabulary a template may draw on.

    Derived from the verdict and nothing else, which is the guarantee that
    makes the copy trustworthy: a template physically cannot mention a
    restaurant's name or a delivery time, because neither is in here.
    """
    context: dict[str, object] = dict(verdict.facts)
    if "payment_held" in context:
        context["money"] = money_sentence(context["payment_held"], context.get("unwind_done", True))
    if verdict.stage_elapsed_s is not None:
        # Whole minutes, floored at one. "0 minutes" is not a duration a
        # person recognises, and every template using a duration is chosen
        # by a bucket that already means "at least a minute".
        minutes = max(1, verdict.stage_elapsed_s // 60)
        context["minutes"] = minutes
        plural = "minute" if minutes == 1 else "minutes"
        context["elapsed"] = f"{minutes} {plural}"
    return context


def render(
    verdict: Verdict, *, locale: str = BASE_LOCALE, cache: TemplateCache | None = None
) -> Explanation:
    """Verdict → the sentence a customer reads. Pure, and model-free."""
    bucket = bucket_for(verdict)
    template = (cache or TemplateCache()).get(verdict.reason, locale, bucket)
    if template is not None:
        try:
            return Explanation(
                text=template.format_map(context_for(verdict)),
                reason=verdict.reason,
                bucket=bucket,
                locale=locale,
            )
        except (KeyError, IndexError):
            # A template naming a fact the verdict does not carry. The
            # customer gets the hand-off rather than a 500; `source` is how
            # anyone finds out this happened.
            pass
    return Explanation(
        text=_HANDOFF, reason=verdict.reason, bucket=bucket, locale=locale, source="fallback"
    )
