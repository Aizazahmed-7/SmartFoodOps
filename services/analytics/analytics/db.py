"""analytics_db — one FACT row per order, folded from lifecycle events.

Design over the obvious alternative (a counters table): counters cannot
absorb at-least-once redelivery — `orders = orders + 1` applied twice is a
lie, and no natural key saves an increment. A fact row CAN: every event
writes absolute values keyed by order_id, so a redelivered batch converges
to the same row. Aggregates (daily rollups, rates, peaks) are computed at
READ time from facts; materializing them back into tables is the named
scale knob, done then as periodic recomputation — never as increments.
"""

from collections.abc import Sequence

import sqlalchemy as sa

metadata = sa.MetaData()


def _slugs() -> sa.types.TypeEngine[Sequence[str]]:
    """A list of ids — ARRAY on Postgres, JSON on sqlite.

    Same split the assistant's index uses, and for the same reason: these
    are a derived read model, so the ids live on the row that is filtered
    rather than in a join table. Catalog, which AUTHORS tags, goes the
    other way on purpose.
    """
    return sa.ARRAY(sa.Text).with_variant(sa.JSON, "sqlite")


order_facts = sa.Table(
    "order_facts",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False, index=True),
    # The branch's brand (ADR-0028), from the event payload; NULL for facts
    # projected before the cutover until the repoint consumer heals them.
    # The owner's read API scopes on (brand_id OR restaurant_id).
    sa.Column("brand_id", sa.Text, nullable=True, index=True),
    sa.Column("user_id", sa.Text, nullable=False),
    # The LATEST lifecycle state seen. Per-order ordering is guaranteed by
    # the topic key (= order_id → one partition), so last-write-wins here
    # is genuinely last-event-wins.
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("total_cents", sa.Integer, nullable=False, server_default="0"),
    # One timestamp per milestone the metrics need. NULL = not reached.
    sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=True, index=True),
    sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    # The courier (dispatch milestone): stamped by order events once
    # assigned — the per-rider delivery spans FR-43's utilization needs.
    sa.Column("rider_id", sa.Text, nullable=True),
    sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("cancel_reason", sa.Text, nullable=True),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# One row per SAMPLED menu view (S8). view_id is uuid5(request_id) minted at
# the emitter, so at-least-once redelivery collapses on this PK — the same
# natural-key dedupe as everything else, applied to telemetry. user_id NULL
# = anonymous browser: counts toward volume, excluded from conversion (you
# cannot join an order to a browser you cannot name).
menu_views = sa.Table(
    "menu_views",
    metadata,
    sa.Column("view_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("user_id", sa.Text, nullable=True),
    sa.Column("viewed_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_menu_views_restaurant_time", menu_views.c.restaurant_id, menu_views.c.viewed_at)


# One row per (order, menu item) — B4's FR-96. `order_facts` carries totals
# and no item ids at all, so recommendation acceptance (FR-79) and taste
# profiles (FR-75) are unbuildable from it: both need to know WHAT was
# ordered, not just how much it cost.
#
# Written once, from OrderPlaced, and never updated. The order's LIFECYCLE
# lives on `order_facts` and is joined to — a cancelled order's items are
# still facts about what was placed, and duplicating `status` here would be
# a second writer for a column that already has one.
order_item_facts = sa.Table(
    "order_item_facts",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    # Quantities are SUMMED across lines that share a menu item. The cart
    # splits a line per option combination, so one order can legitimately
    # carry the same dish twice ("large, no chilli" and "small") — keying on
    # the pair and summing keeps the row an absolute value, which is what
    # makes a redelivered OrderPlaced converge instead of double-count.
    sa.Column("menu_item_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("user_id", sa.Text, nullable=False),
    # The name AS ORDERED. A menu edit must not rewrite history, and the
    # recommender reads ids — this is for humans reading the table.
    sa.Column("name_snapshot", sa.Text, nullable=False),
    sa.Column("qty", sa.Integer, nullable=False),
    sa.Column("line_total_cents", sa.Integer, nullable=False),
    sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
# Popularity (FR-80) reads by item over a window; taste profiles (FR-75)
# read by user. Two access shapes, two indexes — the composite on
# (menu_item_id, placed_at) is what keeps the cold-start query off a scan.
sa.Index("ix_item_facts_restaurant", order_item_facts.c.restaurant_id)
sa.Index("ix_item_facts_item_time", order_item_facts.c.menu_item_id, order_item_facts.c.placed_at)
sa.Index("ix_item_facts_user_time", order_item_facts.c.user_id, order_item_facts.c.placed_at)


# ── B7: the assistant's own facts (FR-94, FR-95) ────────────────────

assistant_facts = sa.Table(
    "assistant_facts",
    metadata,
    # The natural key, and the reason a redelivery converges. The outbox is
    # at-least-once and its poller re-sends anything it published but did
    # not mark, so a fact keyed by anything else — or carrying a delta —
    # would inflate every KPI below each time a poller crashed mid-flight.
    sa.Column("message_id", sa.Text, primary_key=True),
    sa.Column("conversation_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("city", sa.Text, nullable=False),
    # answered | refused | no_match | cold_start | failed, and read by a
    # GROUP BY so a sixth appears on its own. A FAILURE IS A FACT: a KPI
    # that counted only successes would improve every time the provider got
    # worse, which is the direction a metric must never move for free.
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("refusal_reason", sa.Text, nullable=False, server_default="none"),
    # "" when a model produced the answer, otherwise which cache tier served
    # it. Without the split, "average AI response time" (FR-95) averages a
    # 4ms exact-cache hit against a 2s generation and stops describing
    # anything a customer experiences.
    sa.Column("cache_tier", sa.Text, nullable=False, server_default=""),
    # What the answer CITED, not what retrieval found. A conversion credited
    # to a restaurant the customer was never shown is invented (FR-97).
    sa.Column("item_ids", _slugs(), nullable=False),
    sa.Column("restaurant_ids", _slugs(), nullable=False),
    sa.Column("candidates", sa.Integer, nullable=False, server_default="0"),
    # How many candidates the grounding validator dropped (FR-70). A rising
    # number means retrieval is feeding the model things it cannot support.
    sa.Column("ungrounded", sa.Integer, nullable=False, server_default="0"),
    # The WHOLE turn, retrieval included — the wait a customer sits through.
    # A provider-only number would flatter us.
    sa.Column("duration_ms", sa.Float, nullable=False),
    sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
# Usage and response time are read over a window (FR-95); conversion joins
# by user and time inside a bounded window (FR-97).
sa.Index("ix_assistant_facts_time", assistant_facts.c.occurred_at)
sa.Index("ix_assistant_facts_user_time", assistant_facts.c.user_id, assistant_facts.c.occurred_at)


# ── B7: which brand owns which branch (FR-98) ───────────────────────

# A citation names a BRANCH (`rst_…`) because that is what the index
# carries, while a restaurant admin's claim is normally the BRAND
# (`brd_…`) — identity grants `payload.brand_id or aggregate_id`. Without
# this mapping a brand owner would see their AI-driven CONVERSIONS, which
# join through `order_facts.brand_id`, and zero AI-driven VIEWS, which join
# through citations. Half a dashboard, and the wrong half.
#
# A mapping rather than a `brand_ids` column on the facts, which is the
# call worth defending. It is COMPLETE: `catalog.changes` is compacted and
# carries every branch, so every branch is mapped whether or not it has
# ever been cited, ordered from, or reached by the code path that happened
# to produce a citation. And it is CURRENT: a branch repointed to another
# brand re-scopes its history on the next event, which is the same
# intention `repoint_brand` has for the denormalized columns — except here
# it needs no backfill at all.
restaurant_brands = sa.Table(
    "restaurant_brands",
    metadata,
    sa.Column("restaurant_id", sa.Text, primary_key=True),
    sa.Column("brand_id", sa.Text, nullable=False, index=True),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
