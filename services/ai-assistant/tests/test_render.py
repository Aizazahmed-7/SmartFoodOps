"""Explanations from templates (FR-87).

The test that matters most is `test_every_shipped_template_renders`: it
walks the copy registry itself, so a template naming a fact the resolver
never supplies fails here rather than shrugging at a customer who asked
where their food is.
"""

import dataclasses
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ai_assistant.domain.explain import (
    Delivery,
    ReasonCode,
    Timeline,
    Verdict,
    resolve,
)
from ai_assistant.domain.ports import KitchenLoad
from ai_assistant.domain.render import (
    _EN,
    _HANDOFF,
    Bucket,
    TemplateCache,
    bucket_for,
    render,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


def _at(status: str, **stamps) -> Timeline:
    return Timeline(status=status, placed_at=_ago(minutes=40), **stamps)


def _verdict(reason: ReasonCode) -> Verdict:
    """A REAL verdict per reason, produced by the resolver rather than
    hand-built — so the facts a template may draw on are the facts the
    resolver actually attaches, not the ones a test author assumed."""
    cases: dict[ReasonCode, Verdict] = {}
    scenarios: tuple[tuple[Timeline, dict[str, Any]], ...] = (
        (_at("PLACED"), {}),
        (_at("CONFIRMED", confirmed_at=_ago(seconds=30)), {}),
        (_at("ACCEPTED", accepted_at=_ago(minutes=3)), {}),
        (_at("PREPARING", preparing_at=_ago(minutes=8)), {}),
        (
            _at("PREPARING", preparing_at=_ago(minutes=8)),
            {"load": KitchenLoad("rst_1", active=8, capacity=8, as_of=NOW)},
        ),
        (_at("READY", ready_at=_ago(minutes=3)), {}),
        (
            _at("READY", ready_at=_ago(minutes=6)),
            {"delivery": Delivery(state="ASSIGNED", assigned_at=_ago(minutes=2))},
        ),
        (_at("PICKED_UP", picked_up_at=_ago(minutes=7)), {}),
        (_at("DELIVERED", picked_up_at=_ago(minutes=25)), {}),
        (_at("REFUNDED"), {}),
        *(
            (_at("CANCELLED", cancel_reason=r, accepted_at=_ago(minutes=20)), {})
            for r in (
                "customer_cancelled",
                "restaurant_rejected",
                "restaurant_timeout",
                "item_unavailable",
                "at_capacity",
                "payment_declined",
                "no_rider_available",
                "system_timeout",
            )
        ),
        (_at("TELEPORTING"), {}),
    )
    for timeline, kwargs in scenarios:
        verdict = resolve(timeline, now=NOW, **kwargs)
        cases.setdefault(verdict.reason, verdict)
    return cases[reason]


# ── buckets ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("elapsed_s", "expected"),
    [
        (0, Bucket.JUST_NOW),
        (59, Bucket.JUST_NOW),
        (60, Bucket.SHORT),
        (299, Bucket.SHORT),
        (300, Bucket.EXTENDED),
        (899, Bucket.EXTENDED),
        (900, Bucket.LONG),
        (99999, Bucket.LONG),
    ],
)
def test_the_bucket_boundaries_are_where_they_claim_to_be(elapsed_s, expected):
    verdict = Verdict(
        reason=ReasonCode.KITCHEN_PREPARING,
        overdue=False,
        stage="preparing",
        stage_elapsed_s=elapsed_s,
    )
    assert bucket_for(verdict) is expected


def test_overdue_outranks_every_duration():
    """A blown deadline is the salient fact whatever the clock says — a
    restaurant 4 seconds past its window is not "just now"."""
    verdict = Verdict(
        reason=ReasonCode.AWAITING_RESTAURANT,
        overdue=True,
        stage="awaiting_restaurant",
        stage_elapsed_s=184,
    )
    assert bucket_for(verdict) is Bucket.OVERDUE


def test_a_stage_with_no_start_moment_buckets_to_unknown():
    verdict = Verdict(
        reason=ReasonCode.AWAITING_RESTAURANT,
        overdue=None,
        stage="awaiting_restaurant",
        stage_elapsed_s=None,
    )
    assert bucket_for(verdict) is Bucket.UNKNOWN


def test_an_unbudgeted_stage_can_never_reach_overdue():
    """The kitchen and the road have no deadline, so `overdue` is None
    there and nothing can call them late."""
    for reason in (ReasonCode.KITCHEN_PREPARING, ReasonCode.IN_TRANSIT, ReasonCode.KITCHEN_BUSY):
        verdict = _verdict(reason)
        assert verdict.overdue is None
        assert bucket_for(verdict) is not Bucket.OVERDUE


# ── the copy ───────────────────────────────────────────────────────


def test_every_shipped_template_renders():
    """Walks the registry, not a list someone remembered to update. A
    template naming a placeholder the verdict does not carry falls back to
    the hand-off with `source="fallback"`, and that is a failure here."""
    for reason, by_bucket in _EN.items():
        verdict = _verdict(reason)
        for bucket in by_bucket:
            target = bucket if bucket is not None else bucket_for(verdict)
            forced = dataclasses.replace(
                verdict,
                overdue=True if target is Bucket.OVERDUE else verdict.overdue,
                stage_elapsed_s=_ELAPSED_FOR[target],
            )
            explanation = render(forced)
            assert explanation.text, (reason, bucket)
            # `source` rather than the text: an honest UNKNOWN renders the
            # same sentence a failure does, by design.
            assert explanation.source == "template", (reason, bucket)
            assert "{" not in explanation.text, (reason, bucket)


_ELAPSED_FOR = {
    Bucket.JUST_NOW: 20,
    Bucket.SHORT: 120,
    Bucket.EXTENDED: 480,
    Bucket.LONG: 1800,
    Bucket.OVERDUE: 1200,
    Bucket.UNKNOWN: None,
}


def test_every_reason_code_has_copy():
    """A resolver verdict with no template would reach a customer as the
    last-resort shrug. The two modules must stay in step."""
    assert set(_EN) == set(ReasonCode), f"no copy for: {sorted(set(ReasonCode) - set(_EN))}"


def test_the_same_cause_reads_differently_as_time_passes():
    """Why `bucket` is in the key at all."""
    verdict = _verdict(ReasonCode.AWAITING_RESTAURANT)
    fresh = render(dataclasses.replace(verdict, stage_elapsed_s=20)).text
    waiting = render(dataclasses.replace(verdict, stage_elapsed_s=180)).text
    assert "just been sent" in fresh
    assert "for 3 minutes" in waiting
    assert fresh != waiting


def test_a_no_courier_cancel_says_the_food_was_cooked_and_the_money_is_coming_back():
    """The distinction FR-32 owes the customer: their meal was made and
    then binned, which reads very differently from a generic cancellation.

    This test previously asserted "won't be charged" — the exact falsehood
    the resolver's own comment says must never be said to this customer.
    The fixture had no `payment_cleared_at`, so it pinned the bug."""
    verdict = resolve(
        _at(
            "CANCELLED",
            cancel_reason="no_rider_available",
            payment_cleared_at=_ago(minutes=35),
            confirmed_at=_ago(minutes=34),
            accepted_at=_ago(minutes=30),
            ready_at=_ago(minutes=12),
        ),
        now=NOW,
    )
    text = render(verdict).text
    assert "ready" in text
    assert "hold has been released" in text
    assert "won't be charged" not in text


def test_the_busy_kitchen_states_the_load_it_was_given():
    text = render(_verdict(ReasonCode.KITCHEN_BUSY)).text
    assert "at capacity" in text
    assert "8 orders" in text


def test_unknown_hands_off_and_claims_nothing():
    """UC-22: unsure means a support hand-off, never a guess."""
    text = render(_verdict(ReasonCode.UNKNOWN)).text
    assert "Support" in text
    assert "minutes" not in text


def test_durations_are_pluralised_and_never_render_as_zero():
    """ "0 minutes" is not a duration a person recognises, and "1 minutes"
    is a seam a customer notices immediately. Plural rules live in the
    formatter so a locale gets them once, not once per sentence."""
    preparing = _verdict(ReasonCode.KITCHEN_PREPARING)
    assert "for 1 minute." in render(dataclasses.replace(preparing, stage_elapsed_s=61)).text
    # Under a minute the bucket is JUST_NOW, whose copy carries no
    # duration at all — so "0 minutes" is impossible by construction
    # rather than clamped away.
    assert "minute" not in render(dataclasses.replace(preparing, stage_elapsed_s=5)).text
    assert "for 7 minutes" in render(dataclasses.replace(preparing, stage_elapsed_s=430)).text


# ── locale ─────────────────────────────────────────────────────────


def test_an_unknown_locale_falls_back_to_english():
    explanation = render(_verdict(ReasonCode.DELIVERED), locale="ur")
    assert explanation.text == "Your order was delivered."
    assert explanation.locale == "ur"  # what was asked for, honestly recorded


def test_a_partial_translation_degrades_per_bucket_not_wholesale():
    """A locale that translates one bucket keeps English for the rest,
    rather than losing the whole reason."""
    cache = TemplateCache(
        {
            "en": _EN,
            "ur": {ReasonCode.AWAITING_COURIER: {Bucket.JUST_NOW: "Khana tayyar hai."}},
        }
    )
    verdict = _verdict(ReasonCode.AWAITING_COURIER)
    fresh = render(dataclasses.replace(verdict, stage_elapsed_s=10), locale="ur", cache=cache)
    later = render(dataclasses.replace(verdict, stage_elapsed_s=400), locale="ur", cache=cache)
    assert fresh.text == "Khana tayyar hai."
    assert "courier" in later.text  # fell through to en for this bucket


def test_a_template_naming_an_unknown_fact_shrugs_rather_than_crashes():
    """Unreachable for shipped copy (the registry walk above proves it).
    A KeyError reaching a customer who asked where their food is would be
    worse than a shrug."""
    cache = TemplateCache({"en": {ReasonCode.DELIVERED: {None: "Left at {front_door}."}}})
    explanation = render(_verdict(ReasonCode.DELIVERED), cache=cache)
    assert explanation.text == _HANDOFF
    assert explanation.source == "fallback"


def test_a_reason_with_no_copy_at_all_is_a_fallback_not_a_crash():
    explanation = render(_verdict(ReasonCode.DELIVERED), cache=TemplateCache({"en": {}}))
    assert explanation.source == "fallback"


def test_an_honest_unknown_is_not_reported_as_a_failure():
    """The two produce identical prose; only `source` separates them, and
    a caller that conflates them will never learn a template is broken."""
    explanation = render(_verdict(ReasonCode.UNKNOWN))
    assert explanation.text == _HANDOFF
    assert explanation.source == "template"


# ── FR-87's acceptance criterion, structurally ─────────────────────


def test_the_renderer_cannot_reach_a_model():
    """ "With `llm_api_key=""` the feature still answers" is a property of
    the import graph, not a promise. This module may not reach a provider,
    an adapter, or the network at all — so there is no configuration in
    which the template path stops working."""
    source = (
        pathlib.Path(__file__).parent.parent / "ai_assistant" / "domain" / "render.py"
    ).read_text()
    banned = ("httpx", "openai", "anthropic", "adapters", "llm", "requests", "socket")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and any(word in line.lower() for word in banned)
    ]
    assert offenders == [], f"the template floor reaches outward: {offenders}"


# ── the money sentence (FR-86) ─────────────────────────────────────


def test_a_post_authorization_cancel_says_the_hold_was_released():
    """`confirmed_at` IS the evidence: authorization happens before
    CONFIRMED, so a stamped `confirmed_at` means money was held."""
    verdict = resolve(
        _at(
            "CANCELLED",
            cancel_reason="no_rider_available",
            confirmed_at=_ago(minutes=30),
            accepted_at=_ago(minutes=28),
            ready_at=_ago(minutes=12),
        ),
        now=NOW,
    )
    assert verdict.facts["payment_held"] is True
    assert "Your card hold has been released." in render(verdict).text


def test_a_pre_authorization_cancel_says_nothing_was_charged():
    verdict = resolve(_at("CANCELLED", cancel_reason="payment_declined"), now=NOW)
    assert verdict.facts["payment_held"] is False
    assert "You won't be charged." in render(verdict).text


def test_the_money_sentence_is_derived_from_the_authorization_stamp():
    """The evidence is `payment_cleared_at`, not `confirmed_at`.

    `authorize_payment` moves an order to PAYMENT_CLEARED only on a
    successful authorization; CONFIRMED is one transition LATER. The first
    version of this rule used `confirmed_at` and therefore told every
    customer cancelled at PAYMENT_CLEARED — a real hold on a real card —
    that they would not be charged."""
    for reason in (
        "customer_cancelled",
        "restaurant_rejected",
        "restaurant_timeout",
        "no_rider_available",
        "system_timeout",
    ):
        held = resolve(
            _at("CANCELLED", cancel_reason=reason, payment_cleared_at=_ago(minutes=20)), now=NOW
        )
        assert "hold has been released" in render(held).text, reason

    # The three that provably precede any authorization: a declined card is
    # an authorization that FAILED, and stock/capacity refusals happen in
    # validate_and_reserve before payment is called at all.
    for reason in ("payment_declined", "item_unavailable", "at_capacity"):
        never = resolve(_at("CANCELLED", cancel_reason=reason), now=NOW)
        assert "won't be charged" in render(never).text, reason


def test_an_order_cancelled_at_payment_cleared_is_told_its_hold_is_coming_back():
    """The population the first rule got wrong. `workflows.py` cancels from
    PAYMENT_CLEARED with `void=True` — the hold is real — and CONFIRMED was
    never reached, so `confirmed_at` is null."""
    verdict = resolve(
        _at("CANCELLED", cancel_reason="system_timeout", payment_cleared_at=_ago(minutes=5)),
        now=NOW,
    )
    assert verdict.facts["payment_held"] is True
    assert "won't be charged" not in render(verdict).text


def test_a_row_with_no_milestones_at_all_makes_no_money_claim():
    """Orders placed before these columns existed carry no evidence either
    way, and they are never backfilled (ADR-0046). Saying "you won't be
    charged" to someone with a live hold is the worst sentence this engine
    can produce; saying nothing is a smaller failure."""
    verdict = resolve(_at("CANCELLED", cancel_reason="restaurant_rejected"), now=NOW)
    assert verdict.facts["payment_held"] is None
    text = render(verdict).text
    assert "won't be charged" not in text
    assert "hold" not in text
    assert text.strip().endswith("cancelled.")


def test_an_unwind_still_running_does_not_use_the_past_tense():
    """CANCELLING is the unwind IN PROGRESS: the status moves first and the
    void runs after. "Has been released" is false for however long that
    takes, and compensations retry for a long time under a bad PSP."""
    verdict = resolve(
        _at("CANCELLING", cancel_reason="restaurant_rejected", payment_cleared_at=_ago(minutes=6)),
        now=NOW,
    )
    text = render(verdict).text
    assert "is being released" in text
    assert "has been released" not in text


def test_an_overdue_in_flight_stage_does_not_promise_no_charge():
    """These three predict a cancellation, and all three are downstream of
    a successful authorization. They used to hardcode "you won't be
    charged" — to customers whose money was already held."""
    for status, stamps in (
        ("CONFIRMED", {"confirmed_at": _ago(seconds=400)}),
        ("READY", {"ready_at": _ago(minutes=12)}),
    ):
        verdict = resolve(_at(status, payment_cleared_at=_ago(minutes=30), **stamps), now=NOW)
        assert verdict.overdue is True, status
        text = render(verdict).text
        assert "won't be charged" not in text, status
        assert "is being released" in text, status


def test_a_busy_kitchen_never_renders_a_count_larger_than_its_capacity():
    """`active > capacity` is legal — capacity lowered under a running
    kitchen — and "9 of the 8 orders it can take at once" reads as a broken
    system rather than a busy one."""
    load = KitchenLoad(restaurant_id="rst_1", active=9, capacity=8, as_of=NOW)
    for elapsed in (120, 1800):
        verdict = resolve(_at("PREPARING", preparing_at=_ago(seconds=elapsed)), now=NOW, load=load)
        text = render(verdict).text
        assert "9 of" not in text and "of the 8" not in text
        assert "9 orders" in text
