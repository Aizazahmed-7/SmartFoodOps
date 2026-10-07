"""FR-95's six metrics, read from `assistant_facts` and `order_item_facts`.

The arithmetic is the easy part. What these tests actually pin down is the
set of definitional choices a dashboard inherits and cannot see: that a
refusal is not an answer, that a cache hit and a generated answer are
averaged apart, that acceptance is scored per turn inside a bounded window
and only for dishes the answer NAMED, and that an empty window answers null
rather than zero.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from analytics.consumers import AssistantFactsProjector, FactsProjector, ItemFactsProjector
from smartfood_auth import AuthContext, headers_for

SYSTEM = headers_for(AuthContext(sub="svc:ops", roles=frozenset({"system"})))
CUSTOMER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
OWNER = headers_for(
    AuthContext(sub="usr_o", roles=frozenset({"restaurant_admin"}), restaurant_id="rst_1")
)


def _iso(minutes_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _turn(
    message_id="msg_1",
    user="usr_1",
    conversation="cnv_1",
    outcome="answered",
    items=("itm_karahi",),
    restaurants=("rst_1",),
    tier="",
    ms=1800.0,
    at=None,
):
    return {
        "event_type": "AssistantInteraction",
        "aggregate_type": "interaction",
        "aggregate_id": message_id,
        "occurred_at": at or _iso(60),
        "payload": {
            "message_id": message_id,
            "conversation_id": conversation,
            "user_id": user,
            "city": "islamabad",
            "outcome": outcome,
            "refusal_reason": "none" if outcome != "refused" else "off_topic",
            "cache_tier": tier,
            "item_ids": list(items),
            "restaurant_ids": list(restaurants),
            "candidates": 8,
            "ungrounded": 1,
            "duration_ms": ms,
        },
    }


def _order(order_id="ord_1", user="usr_1", item="itm_karahi", at=None, restaurant="rst_1"):
    return {
        "event_type": "OrderPlaced",
        "payload": {
            "order_id": order_id,
            "restaurant_id": restaurant,
            "user_id": user,
            "status": "PLACED",
            "placed_at": at or _iso(30),
            "totals": {"totals": {"total_cents": 1200}},
            "brand_id": None,
            "items": [
                {
                    "menu_item_id": item,
                    "name": "Chicken Karahi",
                    "unit_price_cents": 1200,
                    "qty": 1,
                    "options": [],
                    "line_total_cents": 1200,
                }
            ],
        },
    }


def _settle(order_id="ord_1", at=None, restaurant="rst_1", cents=1200):
    return {
        "event_type": "OrderSettled",
        "payload": {
            "order_id": order_id,
            "restaurant_id": restaurant,
            "user_id": "usr_1",
            "status": "SETTLED",
            "occurred_at": at or _iso(10),
            "totals": {"totals": {"total_cents": cents}},
        },
    }


def _fold(app, turns=(), orders=()):
    """Order events reach BOTH order projections — the same split the live
    stack runs, where each is its own consumer group on one topic."""
    sessions = app.state.service._sessions

    async def run():
        if turns:
            await AssistantFactsProjector(sessions).handle_batch(list(turns))
        if orders:
            await FactsProjector(sessions).handle_batch(list(orders))
            await ItemFactsProjector(sessions).handle_batch(list(orders))

    asyncio.run(run())


def _ai(client, days=7):
    return client.get(f"/v1/internal/analytics/ai?days={days}", headers=SYSTEM).json()


# ── usage, questions, engagement ───────────────────────────────────


def test_usage_counts_turns_users_and_conversations(client, app):
    _fold(
        app,
        turns=[
            _turn("m1", user="usr_1", conversation="cnv_1"),
            _turn("m2", user="usr_1", conversation="cnv_1"),
            _turn("m3", user="usr_2", conversation="cnv_2"),
        ],
    )
    usage = _ai(client)["usage"]
    assert usage == {"interactions": 3, "users": 2, "conversations": 2}


def test_only_the_answered_outcome_counts_as_answered(client, app):
    """A refusal and a no-match are honest replies that answered nothing.
    Folding them in would make the rate RISE as the assistant got less
    useful, which is the direction a metric must never move for free."""
    _fold(
        app,
        turns=[
            _turn("m1", outcome="answered"),
            _turn("m2", outcome="refused"),
            _turn("m3", outcome="no_match"),
            _turn("m4", outcome="failed"),
        ],
    )
    questions = _ai(client)["questions"]
    assert questions["asked"] == 4
    assert questions["answered"] == 1
    assert questions["answer_rate"] == 0.25
    assert questions["by_outcome"] == {"answered": 1, "failed": 1, "no_match": 1, "refused": 1}


def test_an_unknown_outcome_still_appears_in_the_breakdown(client, app):
    """GROUP BY, not a fixed set of counts: an outcome the graph starts
    emitting must show up on its own, or the breakdown silently stops
    summing to the turn count and the gap reads as a drop in usage."""
    _fold(app, turns=[_turn("m1", outcome="answered"), _turn("m2", outcome="budget_exhausted")])
    body = _ai(client)
    assert body["questions"]["by_outcome"] == {"answered": 1, "budget_exhausted": 1}
    assert sum(body["questions"]["by_outcome"].values()) == body["questions"]["asked"]


def test_engagement_counts_returning_users_by_conversation(client, app):
    """usr_1 opens the assistant twice (two conversations) — returning.
    usr_2 has one long conversation — engaged, but not a return visit."""
    _fold(
        app,
        turns=[
            _turn("m1", user="usr_1", conversation="cnv_1"),
            _turn("m2", user="usr_1", conversation="cnv_2"),
            _turn("m3", user="usr_2", conversation="cnv_3"),
            _turn("m4", user="usr_2", conversation="cnv_3"),
        ],
    )
    engagement = _ai(client)["engagement"]
    assert engagement["returning_users"] == 1
    assert engagement["returning_rate"] == 0.5
    assert engagement["turns_per_conversation"] == round(4 / 3, 2)


# ── response time ──────────────────────────────────────────────────


def test_response_time_splits_generated_from_cached(client, app):
    """The blended mean describes neither experience: it drifts with the
    hit rate rather than with the system getting faster."""
    _fold(
        app,
        turns=[
            _turn("m1", tier="", ms=4000.0),
            _turn("m2", tier="", ms=2000.0),
            _turn("m3", tier="exact", ms=10.0),
            _turn("m4", tier="semantic", ms=30.0),
        ],
    )
    timing = _ai(client)["response_time_ms"]
    assert timing["avg"] == 1510.0
    assert timing["avg_generated"] == 3000.0
    assert timing["avg_cached"] == 20.0
    assert timing["cache_hit_rate"] == 0.5
    assert "assistant_response_seconds" in timing["percentiles"]


def test_response_time_is_null_when_a_population_is_empty(client, app):
    """Every turn was a cache hit: there is no generated mean to report,
    and 0.0 would claim the model answers instantly."""
    _fold(app, turns=[_turn("m1", tier="exact", ms=12.0)])
    timing = _ai(client)["response_time_ms"]
    assert timing["avg_cached"] == 12.0
    assert timing["avg_generated"] is None
    assert timing["cache_hit_rate"] == 1.0


# ── recommendation acceptance ──────────────────────────────────────


def test_acceptance_credits_a_dish_the_answer_named(client, app):
    _fold(
        app,
        turns=[_turn("m1", items=("itm_karahi", "itm_biryani"), at=_iso(60))],
        orders=[_order("ord_1", item="itm_biryani", at=_iso(30))],
    )
    recs = _ai(client)["recommendations"]
    assert recs == {
        "turns_naming_items": 1,
        "accepted": 1,
        "acceptance_rate": 1.0,
        "window_hours": 24,
    }


def test_acceptance_is_scored_per_turn_not_per_item(client, app):
    """One answer naming five dishes is ONE recommendation a customer acts
    on or does not. Per item it would cap at 20% for behaving perfectly, so
    the rate would fall every time the answers got more helpful."""
    _fold(
        app,
        turns=[_turn("m1", items=tuple(f"itm_{n}" for n in range(5)), at=_iso(60))],
        orders=[_order("ord_1", item="itm_3", at=_iso(30))],
    )
    assert _ai(client)["recommendations"]["acceptance_rate"] == 1.0


def test_a_dish_the_answer_never_named_is_not_an_acceptance(client, app):
    """Cited, not retrieved. A customer who ordered something else was not
    taking the recommendation."""
    _fold(
        app,
        turns=[_turn("m1", items=("itm_karahi",), at=_iso(60))],
        orders=[_order("ord_1", item="itm_pizza", at=_iso(30))],
    )
    recs = _ai(client)["recommendations"]
    assert recs["turns_naming_items"] == 1 and recs["accepted"] == 0
    assert recs["acceptance_rate"] == 0.0  # measured zero, not missing data


def test_another_customers_order_is_not_an_acceptance(client, app):
    _fold(
        app,
        turns=[_turn("m1", user="usr_1", at=_iso(60))],
        orders=[_order("ord_1", user="usr_2", at=_iso(30))],
    )
    assert _ai(client)["recommendations"]["accepted"] == 0


def test_an_order_outside_the_window_is_not_an_acceptance(client, app):
    """Bounded attribution: 25 hours later is a customer who felt like
    karahi, not an answer that persuaded them."""
    _fold(
        app,
        turns=[_turn("m1", at=_iso(60 * 26))],
        orders=[_order("ord_1", at=_iso(30))],
    )
    assert _ai(client, days=7)["recommendations"]["accepted"] == 0


def test_an_order_before_the_turn_is_not_an_acceptance(client, app):
    """The window is one-sided. A dish already in the cart cannot have been
    accepted from an answer that had not been written yet."""
    _fold(
        app,
        turns=[_turn("m1", at=_iso(30))],
        orders=[_order("ord_1", at=_iso(60))],
    )
    assert _ai(client)["recommendations"]["accepted"] == 0


def test_a_turn_naming_nothing_is_not_in_the_denominator(client, app):
    """ "How's my order?" recommended nothing and cannot be accepted or
    refused; counting it would drag the rate toward zero with traffic."""
    _fold(
        app,
        turns=[_turn("m1", items=()), _turn("m2", items=("itm_karahi",))],
        orders=[_order("ord_1", item="itm_karahi", at=_iso(30))],
    )
    recs = _ai(client)["recommendations"]
    assert recs["turns_naming_items"] == 1 and recs["acceptance_rate"] == 1.0


def test_an_anonymous_turn_is_excluded_from_acceptance(client, app):
    """There should be no empty user_id — the chat route is authenticated.
    If one ever appears it would join to every anonymous order row and
    credit the assistant with strangers' dinners."""
    _fold(
        app,
        turns=[_turn("m1", user="", at=_iso(60))],
        orders=[_order("ord_1", user="", at=_iso(30))],
    )
    recs = _ai(client)["recommendations"]
    assert recs["turns_naming_items"] == 0 and recs["acceptance_rate"] is None


# ── windowing, honesty, access ─────────────────────────────────────


def test_the_window_excludes_older_turns(client, app):
    _fold(app, turns=[_turn("m1", at=_iso(60)), _turn("m2", at=_iso(60 * 24 * 9))])
    assert _ai(client, days=7)["usage"]["interactions"] == 1
    assert _ai(client, days=30)["usage"]["interactions"] == 2


def test_an_empty_window_answers_nulls_not_zeros(client):
    body = _ai(client, days=1)
    assert body["usage"] == {"interactions": 0, "users": 0, "conversations": 0}
    assert body["questions"]["answer_rate"] is None
    assert body["response_time_ms"]["avg"] is None
    assert body["recommendations"]["acceptance_rate"] is None
    assert body["order_conversion"]["conversion_rate"] is None
    assert body["order_conversion"]["attributed_revenue_cents"] == 0
    assert body["engagement"]["turns_per_conversation"] is None
    assert body["engagement"]["returning_rate"] is None


def test_grounding_columns_are_read(client, app):
    """7.1 added `candidates` and `ungrounded` for the grounding alert. An
    unread column is how c1.assistant.events sat unconsumed for four
    milestones."""
    _fold(app, turns=[_turn("m1"), _turn("m2")])
    assert _ai(client)["grounding"] == {"candidates_retrieved": 16, "dropped_ungrounded": 2}


def test_ai_metrics_is_system_only(client):
    assert client.get("/v1/internal/analytics/ai", headers=CUSTOMER).status_code == 403
    assert client.get("/v1/internal/analytics/ai", headers=OWNER).status_code == 403


def test_ai_metrics_days_bounds(client):
    assert client.get("/v1/internal/analytics/ai?days=0", headers=SYSTEM).status_code == 422
    assert client.get("/v1/internal/analytics/ai?days=91", headers=SYSTEM).status_code == 422


# ── order conversion after an interaction (FR-97) ──────────────────


def _conv(client, days=7):
    return _ai(client, days)["order_conversion"]


def test_an_order_at_a_cited_restaurant_converts(client, app):
    _fold(
        app,
        turns=[_turn("m1", at=_iso(60))],
        orders=[_order("ord_1", at=_iso(30))],
    )
    conv = _conv(client)
    assert conv["turns_naming_restaurants"] == 1
    assert conv["converted_turns"] == 1
    assert conv["conversion_rate"] == 1.0
    assert conv["attributed_orders"] == 1


def test_an_order_elsewhere_does_not_convert(client, app):
    _fold(
        app,
        turns=[_turn("m1", at=_iso(60))],  # cites rst_1
        orders=[_order("ord_1", restaurant="rst_2", at=_iso(30))],
    )
    conv = _conv(client)
    assert conv["converted_turns"] == 0 and conv["attributed_orders"] == 0


def test_a_matching_order_elsewhere_cannot_attribute_this_one(client, app):
    """The correlation guard, as a test.

    Both facts exist in the table — usr_2 DID order at the cited rst_1 —
    but usr_1's dinner was at rst_2 and belongs to nobody's answer. An
    uncorrelated subquery asks "did ANY order match?" instead of "did THIS
    one?", which is true almost always and inflates every conversion.
    """
    _fold(
        app,
        turns=[
            _turn("m1", user="usr_1", at=_iso(60)),
            _turn("m2", user="usr_2", conversation="cnv_2", at=_iso(60)),
        ],
        orders=[
            _order("ord_1", user="usr_1", restaurant="rst_2", at=_iso(30)),
            _order("ord_2", user="usr_2", restaurant="rst_1", at=_iso(30)),
        ],
    )
    conv = _conv(client)
    assert conv["attributed_orders"] == 1  # usr_2's only — not usr_1's
    assert conv["converted_turns"] == 1


def test_another_customers_order_does_not_convert(client, app):
    _fold(
        app,
        turns=[_turn("m1", user="usr_1", at=_iso(60))],
        orders=[_order("ord_1", user="usr_2", at=_iso(30))],
    )
    assert _conv(client)["converted_turns"] == 0


def test_conversion_respects_the_window_on_both_sides(client, app):
    """25 hours later is a customer who felt like dinner; an order placed
    before the answer existed cannot have been caused by it."""
    _fold(
        app,
        turns=[
            _turn("late", user="usr_a", at=_iso(60 * 26)),
            _turn("early", user="usr_b", at=_iso(30)),
        ],
        orders=[
            _order("ord_1", user="usr_a", at=_iso(30)),
            _order("ord_2", user="usr_b", at=_iso(60)),
        ],
    )
    conv = _conv(client, days=7)
    assert conv["turns_naming_restaurants"] == 2 and conv["converted_turns"] == 0
    assert conv["attributed_orders"] == 0


def test_three_turns_and_one_dinner_are_one_attributed_order(client, app):
    """The double-count guard, and the reason there are two numbers.

    Three turns that pointed at the restaurant all worked. They produced
    ONE dinner. Reporting the turn-side numerator as orders would claim
    three, and the revenue beside it would be tripled.
    """
    _fold(
        app,
        turns=[
            _turn("m1", at=_iso(90)),
            _turn("m2", at=_iso(80)),
            _turn("m3", at=_iso(70)),
        ],
        orders=[_order("ord_1", at=_iso(30))],
    )
    conv = _conv(client)
    assert conv["converted_turns"] == 3
    assert conv["conversion_rate"] == 1.0
    assert conv["attributed_orders"] == 1


def test_attributed_revenue_counts_settled_money_only(client, app):
    """An order the kitchen rejected still converted; it just never became
    money. The house rule the lifetime block already follows."""
    _fold(
        app,
        turns=[
            _turn("m1", user="usr_1", at=_iso(90)),
            _turn("m2", user="usr_2", conversation="cnv_2", at=_iso(90)),
        ],
        orders=[
            _order("ord_1", user="usr_1", at=_iso(60)),
            _order("ord_2", user="usr_2", at=_iso(60)),
            _settle("ord_1", at=_iso(20)),
        ],
    )
    conv = _conv(client)
    assert conv["attributed_orders"] == 2  # both converted
    assert conv["attributed_revenue_cents"] == 1200  # only one settled


def test_an_unattributed_order_contributes_no_revenue(client, app):
    _fold(app, orders=[_order("ord_1", at=_iso(60)), _settle("ord_1", at=_iso(20))])
    conv = _conv(client)
    assert conv["attributed_orders"] == 0 and conv["attributed_revenue_cents"] == 0


def test_a_turn_naming_no_restaurant_is_not_in_the_denominator(client, app):
    """An order-status question pointed the customer nowhere, so it can
    neither convert nor fail to."""
    _fold(
        app,
        turns=[_turn("m1", items=(), restaurants=(), at=_iso(60))],
        orders=[_order("ord_1", at=_iso(30))],
    )
    conv = _conv(client)
    assert conv["turns_naming_restaurants"] == 0 and conv["conversion_rate"] is None


def test_an_anonymous_turn_is_excluded_from_conversion(client, app):
    _fold(
        app,
        turns=[_turn("m1", user="", at=_iso(60))],
        orders=[_order("ord_1", user="", at=_iso(30))],
    )
    conv = _conv(client)
    assert conv["turns_naming_restaurants"] == 0 and conv["attributed_orders"] == 0


def test_conversion_says_what_it_is(client, app):
    """The caveat ships in the payload, not only in a docstring: the number
    travels further than the code does."""
    _fold(app, turns=[_turn("m1")])
    assert "not a controlled measurement" in _conv(client)["basis"]
