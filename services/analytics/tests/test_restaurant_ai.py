"""FR-98 — one owner's AI insights, claim-scoped.

The interesting property is not the arithmetic, it is the SCOPE. A
citation names a BRANCH, because that is what the assistant's index
carries, and a restaurant admin's claim is normally the BRAND — identity
grants `payload.brand_id or aggregate_id`. So the test that matters most is
the plain one: a brand owner can see a turn that named one of their
branches. Without the mapping they would see their conversions, which join
through `order_facts.brand_id`, and none of their views.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from analytics.consumers import AssistantFactsProjector, BrandRepointHandler, FactsProjector
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
SYSTEM = headers_for(AuthContext(sub="svc:ops", roles=frozenset({"system"})))


def _owner(claim: str):
    return headers_for(
        AuthContext(
            sub=f"usr_owner_{claim}", roles=frozenset({"restaurant_admin"}), restaurant_id=claim
        )
    )


def _iso(minutes_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _turn(message_id="m1", user="usr_1", cited=("rst_1",), at=None, conversation="cnv_1"):
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
            "outcome": "answered",
            "refusal_reason": "none",
            "cache_tier": "",
            "item_ids": ["itm_1"],
            "restaurant_ids": list(cited),
            "candidates": 4,
            "ungrounded": 0,
            "duration_ms": 1200.0,
        },
    }


def _order(order_id="ord_1", user="usr_1", restaurant="rst_1", brand=None, at=None, cents=1500):
    return {
        "event_type": "OrderPlaced",
        "payload": {
            "order_id": order_id,
            "restaurant_id": restaurant,
            "brand_id": brand,
            "user_id": user,
            "status": "PLACED",
            "placed_at": at or _iso(30),
            "totals": {"totals": {"total_cents": cents}},
        },
    }


def _settle(order_id="ord_1", restaurant="rst_1", brand=None, at=None, cents=1500):
    return {
        "event_type": "OrderSettled",
        "payload": {
            "order_id": order_id,
            "restaurant_id": restaurant,
            "brand_id": brand,
            "user_id": "usr_1",
            "status": "SETTLED",
            "occurred_at": at or _iso(10),
            "totals": {"totals": {"total_cents": cents}},
        },
    }


def _brand_of(branch: str, brand: str):
    """One `catalog.changes` entry — the compacted topic every branch is on."""
    return {
        "aggregate_type": "restaurant",
        "aggregate_id": branch,
        "payload": json.dumps({"owner_user_id": "u", "brand_id": brand}),
    }


def _fold(app, turns=(), orders=(), brands=()):
    sessions = app.state.service._sessions

    async def run():
        if turns:
            await AssistantFactsProjector(sessions).handle_batch(list(turns))
        if orders:
            await FactsProjector(sessions).handle_batch(list(orders))
        # catalog.changes LAST: in production the facts are already there
        # and the compacted topic heals them, not the other way round.
        for event in brands:
            await BrandRepointHandler(sessions).handle(event)

    asyncio.run(run())


def _ai(client, claim, days=14):
    return client.get(f"/v1/restaurant/analytics/ai?days={days}", headers=_owner(claim)).json()


# ── the scope ──────────────────────────────────────────────────────


def test_a_brand_owner_sees_a_turn_that_named_their_branch(client, app):
    """The whole point of the mapping. The citation says `rst_1`; the claim
    says `brd_1`; identity issues the brand."""
    _fold(app, brands=[_brand_of("rst_1", "brd_1")], turns=[_turn("m1", cited=("rst_1",))])
    assert _ai(client, "brd_1")["views"] == {"turns": 1, "customers": 1}


def test_a_branch_claim_still_works_without_a_mapping(client, app):
    """An older token, or a restaurant with no brand: the direct arm."""
    _fold(app, turns=[_turn("m1", cited=("rst_1",))])
    assert _ai(client, "rst_1")["views"]["turns"] == 1


def test_another_brand_sees_none_of_it(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1"), _brand_of("rst_2", "brd_2")],
        turns=[_turn("m1", cited=("rst_1",))],
    )
    assert _ai(client, "brd_2")["views"] == {"turns": 0, "customers": 0}


def test_a_brand_sees_every_branch_it_owns(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1"), _brand_of("rst_2", "brd_1")],
        turns=[
            _turn("m1", cited=("rst_1",)),
            _turn("m2", cited=("rst_2",), conversation="cnv_2"),
            _turn("m3", cited=("rst_9",), conversation="cnv_3"),
        ],
    )
    assert _ai(client, "brd_1")["views"]["turns"] == 2


def test_a_repoint_moves_the_history(client, app):
    """A mapping rather than a column on the fact, so a branch that changes
    hands re-scopes what the assistant already said about it — the same
    intention `repoint_brand` has, with no backfill."""
    _fold(app, brands=[_brand_of("rst_1", "brd_1")], turns=[_turn("m1", cited=("rst_1",))])
    assert _ai(client, "brd_1")["views"]["turns"] == 1
    _fold(app, brands=[_brand_of("rst_1", "brd_2")])
    assert _ai(client, "brd_1")["views"]["turns"] == 0
    assert _ai(client, "brd_2")["views"]["turns"] == 1


def test_one_turn_naming_two_restaurants_counts_for_both(client, app):
    """A comparison answer is a view for everyone it named."""
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1"), _brand_of("rst_2", "brd_2")],
        turns=[_turn("m1", cited=("rst_1", "rst_2"))],
    )
    assert _ai(client, "brd_1")["views"]["turns"] == 1
    assert _ai(client, "brd_2")["views"]["turns"] == 1


# ── conversions, scoped on both ends ───────────────────────────────


def test_a_turn_that_named_me_and_an_order_at_me_converts(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1")],
        turns=[_turn("m1", cited=("rst_1",), at=_iso(60))],
        orders=[_order("ord_1", restaurant="rst_1", brand="brd_1", at=_iso(30))],
    )
    conv = _ai(client, "brd_1")["conversions"]
    assert conv["converted_turns"] == 1
    assert conv["conversion_rate"] == 1.0
    assert conv["attributed_orders"] == 1


def test_a_turn_that_named_me_and_sent_them_elsewhere_is_not_mine(client, app):
    """Scoped on BOTH ends. Being mentioned is not a conversion."""
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1"), _brand_of("rst_2", "brd_2")],
        turns=[_turn("m1", cited=("rst_1", "rst_2"), at=_iso(60))],
        orders=[_order("ord_1", restaurant="rst_2", brand="brd_2", at=_iso(30))],
    )
    mine = _ai(client, "brd_1")["conversions"]
    # Asserting ONLY attributed_orders is what let this through the first
    # time: converted_turns was 1 and conversion_rate 1.0 off a dinner the
    # competitor served, so brd_1's dashboard read "100% conversion, zero
    # orders" — self-contradictory on its face, and a window onto a
    # co-mentioned competitor's traffic.
    assert mine["attributed_orders"] == 0
    assert mine["converted_turns"] == 0
    assert mine["conversion_rate"] == 0.0
    assert _ai(client, "brd_2")["conversions"]["attributed_orders"] == 1


def test_an_order_i_won_without_the_assistant_is_not_attributed(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1")],
        orders=[_order("ord_1", restaurant="rst_1", brand="brd_1", at=_iso(30))],
    )
    conv = _ai(client, "brd_1")["conversions"]
    assert conv["attributed_orders"] == 0 and conv["attributed_revenue_cents"] == 0


def test_my_revenue_counts_settled_money_only(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1")],
        turns=[
            _turn("m1", user="usr_1", at=_iso(90)),
            _turn("m2", user="usr_2", at=_iso(90), conversation="cnv_2"),
        ],
        orders=[
            _order("ord_1", user="usr_1", brand="brd_1", at=_iso(60)),
            _order("ord_2", user="usr_2", brand="brd_1", at=_iso(60)),
            _settle("ord_1", brand="brd_1", at=_iso(20)),
        ],
    )
    conv = _ai(client, "brd_1")["conversions"]
    assert conv["attributed_orders"] == 2
    assert conv["attributed_revenue_cents"] == 1500


def test_three_questions_and_one_dinner_is_one_order_for_me_too(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1")],
        turns=[
            _turn("m1", at=_iso(90)),
            _turn("m2", at=_iso(85)),
            _turn("m3", at=_iso(80)),
        ],
        orders=[_order("ord_1", brand="brd_1", at=_iso(30))],
    )
    conv = _ai(client, "brd_1")["conversions"]
    assert conv["converted_turns"] == 3 and conv["attributed_orders"] == 1


def test_a_legacy_order_without_a_brand_column_still_counts(client, app):
    """The order facts predate the cutover and carry NULL brand_id; the
    repoint handler heals them, which is why `_scoped` has two arms."""
    _fold(
        app,
        turns=[_turn("m1", cited=("rst_1",), at=_iso(60))],
        orders=[_order("ord_1", restaurant="rst_1", brand=None, at=_iso(30))],
        brands=[_brand_of("rst_1", "brd_1")],
    )
    assert _ai(client, "brd_1")["conversions"]["attributed_orders"] == 1


# ── access, windows, honesty ───────────────────────────────────────


def test_the_window_bounds_it(client, app):
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1")],
        turns=[_turn("m1", at=_iso(60)), _turn("m2", at=_iso(60 * 24 * 20), conversation="cnv_2")],
    )
    assert _ai(client, "brd_1", days=14)["views"]["turns"] == 1
    assert _ai(client, "brd_1", days=30)["views"]["turns"] == 2


def test_an_empty_window_answers_nulls_not_zeros(client, app):
    _fold(app, brands=[_brand_of("rst_1", "brd_1")])
    body = _ai(client, "brd_1", days=1)
    assert body["views"] == {"turns": 0, "customers": 0}
    assert body["conversions"]["conversion_rate"] is None
    assert body["conversions"]["attributed_orders"] == 0


def test_it_says_what_it_is(client, app):
    _fold(app, brands=[_brand_of("rst_1", "brd_1")], turns=[_turn("m1")])
    assert "not a controlled measurement" in _ai(client, "brd_1")["basis"]


def test_it_requires_the_role_and_the_claim(client):
    assert client.get("/v1/restaurant/analytics/ai", headers=CUSTOMER).status_code == 403
    no_claim = headers_for(AuthContext(sub="usr_o", roles=frozenset({"restaurant_admin"})))
    assert client.get("/v1/restaurant/analytics/ai", headers=no_claim).status_code == 404
    # `system` passes the role gate (it passes every one) and then has no
    # claim to be scoped to — the same 404 the FR-55 view answers, and the
    # reason an ops read has its OWN endpoint rather than a tenant one.
    assert client.get("/v1/restaurant/analytics/ai", headers=SYSTEM).status_code == 404


def test_days_bounds(client):
    owner = _owner("brd_1")
    assert client.get("/v1/restaurant/analytics/ai?days=0", headers=owner).status_code == 422
    assert client.get("/v1/restaurant/analytics/ai?days=91", headers=owner).status_code == 422


def test_a_sibling_branch_order_still_converts_for_the_brand(client, app):
    """The same predicate was wrong in the other direction too. The
    assistant named one branch; the customer ordered from another branch of
    the SAME brand. That is the brand's AI-driven conversion — scoping it to
    the branch the answer happened to name silently lost it."""
    _fold(
        app,
        brands=[_brand_of("rst_1", "brd_1"), _brand_of("rst_2", "brd_1")],
        turns=[_turn("m1", cited=("rst_1",), at=_iso(60))],
        orders=[_order("ord_1", restaurant="rst_2", brand="brd_1", at=_iso(30))],
    )
    conv = _ai(client, "brd_1")["conversions"]
    assert conv["converted_turns"] == 1 and conv["attributed_orders"] == 1
