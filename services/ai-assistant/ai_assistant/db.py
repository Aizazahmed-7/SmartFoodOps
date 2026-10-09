"""assistant_db schema.

B0 creates only the outbox. That looks premature — nothing publishes yet —
but it is the cheapest moment to prove the whole persistence path end to
end (initdb create_db -> Alembic at startup -> /readyz SELECT 1), rather
than discovering it is broken during B1 while also debugging embeddings.
The AI plane's facts are a product KPI, so they publish through the outbox
like every other domain fact (ADR-0002, PRD FR-94) — never direct to Kafka.

conversations/messages (B3), taste_profiles (B4) and content_drafts (B6)
land in their own migrations. Everything here must stay sqlite-creatable:
the unit suite runs `metadata.create_all` on sqlite, so Postgres-only types
arrive behind a dialect split — see `embedding` and the array columns below.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from smartfood_outbox import outbox_table
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

outbox = outbox_table(metadata)


# ── B1: menu knowledge (ADR-0032) ───────────────────────────────────

EMBEDDING_DIMENSIONS = 512
"""Vector width, FIXED IN THE DDL.

`Settings.embedding_dimensions` defaults from this constant rather than the
other way round, because a width change is a migration plus a rolling
reindex (FR-61) — never an env flip. Two sources of truth here would mean a
service that asks the provider for 1536 floats and a column that stores 512;
a test pins the migration's literal to this name so they cannot drift.
"""

VECTOR_OPS = "vector_cosine_ops"
"""Cosine distance (`<=>`). These embeddings arrive normalised, so cosine
and inner product rank identically — cosine is the conventional pairing for
`<=>` and the one the retrieval SQL will be written against. The opclass
must match the query operator VERBATIM or the planner ignores the index."""

ITEM_HNSW_INDEX = "ix_item_chunks_embedding_hnsw"
RESTAURANT_HNSW_INDEX = "ix_restaurant_chunks_embedding_hnsw"
"""Created in the migration ONLY: HNSW is Postgres-only DDL, and the unit
suite runs `metadata.create_all` on sqlite. Declaring them here would put a
meaningless B-tree over a JSON column in every unit run."""


def _vector() -> sa.types.TypeEngine[Any]:
    """pgvector on Postgres, JSON on sqlite. Both dialects bind and return a
    plain `list[float]`, so the ingestion path stays dialect-agnostic; only
    the DISTANCE query is Postgres-only, and that runs against a stub session
    in units exactly like catalog's FTS SQL."""
    return Vector(EMBEDDING_DIMENSIONS).with_variant(sa.JSON, "sqlite")


def _slugs() -> sa.types.TypeEngine[Sequence[str]]:
    """A set of lowercase slugs — tags, cuisines — as an ARRAY on Postgres
    and JSON on sqlite.

    This DIVERGES from catalog, which deliberately models cuisines and tags
    as join tables (`restaurant_cuisines`, `item_tags`) and says so in its
    own schema. The reason the same call goes the other way here: catalog is
    the AUTHORING store, where a tag is an entity you insert, rename and
    index exactly. These tables are a DERIVED READ INDEX, and their filters
    are applied by a post-filtered ANN scan — the planner walks the HNSW
    graph and evaluates the predicate per candidate it touches. A predicate
    that reads another row is a per-candidate subquery inside that walk; a
    predicate on the same row is a memory access. Denormalising is the point
    of this table, and a rebuild from the log (FR-59) is what makes the
    duplication safe.
    """
    return sa.ARRAY(sa.Text).with_variant(sa.JSON, "sqlite")


# Two tables, not one table with a `kind` discriminator.
#
# The obvious shape was one `menu_chunks` table with `item_id`, `category`,
# `tags` and `price_cents` NULL on restaurant rows. Three things argue
# against it, and the first is the one that actually decides it:
#
# 1. The two vector spaces are not comparable. An item chunk reads
#    "Chicken Biryani | spicy, halal | Mains"; a restaurant chunk reads
#    "Biryani House | pakistani, bbq | Springfield". A query embedding sits
#    systematically closer to one SHAPE than the other, so a single
#    `ORDER BY embedding <=> q` across both ranks a mediocre restaurant
#    above an excellent dish for reasons that have nothing to do with the
#    question. They must be retrieved separately and fused (RRF, FR-62),
#    which means two queries either way — and two queries want two indexes.
# 2. Smaller, denser HNSW graphs. Each index covers one text shape, and
#    neither pays for the other's rows.
# 3. NOT NULL says what it means. Every column below is required; nothing
#    has to be read as "required, but only for half the rows".

item_chunks = sa.Table(
    "item_chunks",
    metadata,
    # `rst_9:itm_4` — one row per chunk, one generation. The embedding
    # model and its dimensions are fixed by Settings, so there is no second
    # vector space for this key to disambiguate against.
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("item_id", sa.Text, nullable=False),
    # The dish's name as a COLUMN, not only as the first line of `content`.
    # The content studio writes copy from a dish's name, tags, category and
    # cuisine (FR-88) — the other three were already columns, and a
    # consumer that needs the fourth should not parse a blob to get it.
    # NOT NULL like every other column here: the chunk tables hold the
    # contract that each column means something for every row, and a dish
    # without a name is not a dish. Backfilled from `content`'s first line
    # in 0015, which is where it already was.
    sa.Column("name", sa.Text, nullable=False),
    # Hard predicates (FR-62), stored as columns precisely so the database
    # can apply them: a filter that lives in the prose is a filter the
    # database cannot use. Brand TEMPLATE rows are never chunked at all, so
    # nothing here can leak one into a result set (FR-63).
    sa.Column("city", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("cuisines", _slugs(), nullable=False),
    sa.Column("category", sa.Text, nullable=False),
    sa.Column("tags", _slugs(), nullable=False),
    # VOLATILE PRE-FILTERS, and the distinction is load-bearing. These make
    # "under $10" and "not 86'd" cheap to express in SQL instead of an
    # over-fetch, but they are up to one debounce window stale (NFR-28) and
    # are NEVER what the customer is shown: FR-60 requires every item the
    # assistant names to be re-resolved through catalog's cache-bypassing
    # snapshot endpoint, and a candidate that drifted over budget or went
    # unavailable is dropped THERE. Narrowing here, truth there.
    sa.Column("price_cents", sa.Integer, nullable=False),
    sa.Column("available", sa.Boolean, nullable=False),
    # `open` | `paused` (catalog's vocabulary). Denormalised onto the item
    # row so the item leg needs no join, and stored rather than acted on so
    # a pause/resume is a column flip instead of a delete-and-re-embed
    # cycle. Hours-based open/closed is NOT here: it is computed from
    # `hours` + `timezone` and belongs to the same live re-resolution as
    # price (FR-63).
    sa.Column("status", sa.Text, nullable=False),
    # The exact text that was embedded — durable facts only, no price and no
    # availability (FR-60). Kept because the reindex re-embeds from HERE (no
    # catalog call, no Kafka replay) and because B2's lexical leg reads it.
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("embedding", _vector(), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

restaurant_chunks = sa.Table(
    "restaurant_chunks",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("city", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("cuisines", _slugs(), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("embedding", _vector(), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# Retrieval's hard-predicate prefix. `city` leads because every query is
# geo-scoped (FR-63); there is no generation column in front of it any more,
# which only ever held one value and so contributed no selectivity.
sa.Index("ix_item_chunks_scope", item_chunks.c.city)
sa.Index("ix_restaurant_chunks_scope", restaurant_chunks.c.city)
# The ingestion path's own access pattern: hashes_for() and the reconcile
# delete both address one restaurant at one version. BOTH tables need it —
# hashes_for reads them together, and without the second index every drain
# pass sequentially scans restaurant_chunks (one row per branch, so a full
# scan per restaurant indexed).
sa.Index("ix_item_chunks_restaurant", item_chunks.c.restaurant_id)
sa.Index("ix_restaurant_chunks_restaurant", restaurant_chunks.c.restaurant_id)


# ── B1: the debounce queue (FR-57) ──────────────────────────────────

knowledge_pending = sa.Table(
    "knowledge_pending",
    metadata,
    # One row per restaurant, upserted. This IS the debounce: the ADR-0028
    # fan-out means one base-menu edit stages a full-state event per branch,
    # and a burst of twelve events for one restaurant should cost one
    # embedding pass, not twelve.
    sa.Column("restaurant_id", sa.Text, primary_key=True),
    # The whole catalog payload. Catalog's events are full-state snapshots,
    # so the LATEST one is sufficient on its own — the drain needs no
    # history, no catalog call, and cannot read a state newer than the
    # event it is acting on.
    # Also the guard on the drain's delete. The drain reads this row, spends
    # seconds embedding outside any transaction, and must not then delete
    # work that arrived while it was away — so it deletes WHERE the payload
    # is still the one it drained. JSONB equality is semantic, so a payload
    # Postgres re-ordered on write still matches; sqlite's JSON round-trips
    # deterministically. ADR-0039's rule exactly: key the write on the state
    # it protects.
    sa.Column("payload", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
    # A FIXED window, not a sliding one: on conflict this keeps the EARLIER
    # due_at, so the clock starts at the first unprocessed change. A trailing
    # debounce would restart on every keystroke of a long menu edit and could
    # starve past NFR-28's 60 s freshness budget; this bounds staleness at
    # the window plus drain time by construction.
    sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # Never updated on conflict — it is what makes "this restaurant has been
    # waiting 4 minutes" answerable when someone asks why search is stale.
    sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_knowledge_pending_due", knowledge_pending.c.due_at)


# ── B3: the conversation store (ADR-0042) ───────────────────────────

conversations = sa.Table(
    "conversations",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    # An OPAQUE id, never a name or an email (FR-73, NFR-12). Nothing in
    # this schema is a join key into identity; it is the subject a budget is
    # counted against and the owner a read is authorised for.
    sa.Column("user_id", sa.Text, nullable=False),
    # The retrieval scope for every turn in this conversation. Held here
    # rather than passed per message so a follow-up question inherits the
    # city the customer was actually browsing, instead of silently widening.
    sa.Column("city", sa.Text, nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_conversations_user", conversations.c.user_id, conversations.c.updated_at)

messages = sa.Table(
    "messages",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    # CASCADE, and it is the point: NFR-32 purges a conversation after 90
    # days, and a retention rule that requires remembering to delete a
    # second table is a rule that fails an audit rather than a test.
    sa.Column(
        "conversation_id",
        sa.Text,
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("role", sa.Text, nullable=False),  # user | assistant
    # The ASSEMBLED answer. Nothing downstream reconstructs a message by
    # concatenating chunks (ADR-0042 §6): chunks serve reconnects, this is
    # the durable record.
    sa.Column("content", sa.Text, nullable=False, server_default=""),
    # streaming | complete | failed. A reconnect reads this to know whether
    # to replay-and-close or replay-and-follow, and it is what makes a
    # turn that died mid-generation distinguishable from one still running.
    sa.Column("status", sa.Text, nullable=False),
    # What the answer CITED, after grounding dropped the fabrications
    # (FR-70). Stored rather than re-derived: the markers are stripped from
    # `content` before anybody reads it, so the ids are unrecoverable from
    # the text — and a reader who reconnects after the turn finished would
    # otherwise get the prose with no cards under it. A list, not a table:
    # it is read whole, written once, and never queried by member.
    sa.Column("item_ids", sa.JSON, nullable=False, server_default="[]"),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_messages_conversation", messages.c.conversation_id, messages.c.created_at)

message_chunks = sa.Table(
    "message_chunks",
    metadata,
    sa.Column(
        "message_id", sa.Text, sa.ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    ),
    # Monotonic per message, and the composite PK is what enforces it: a
    # producer that reused a seq would corrupt a reconnect silently
    # (ADR-0042), so the database refuses instead.
    sa.Column("seq", sa.Integer, primary_key=True),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)


"""One index over every cached question, not one per fence.

The same reasoning as ADR-0032 §5: a partial index per city would be DDL
keyed on data, and `city` is a cheap filter on top of an ANN scan. The fence
columns are indexed separately so the planner can use either.
"""


# ── B4: order features (FR-75, FR-80) ───────────────────────────────

order_items = sa.Table(
    "order_items",
    metadata,
    # The same pair analytics keys its item facts on, and for the same
    # reason: one order can carry a dish twice (the cart splits a line per
    # option combination), and the pair keeps the row an absolute value that
    # a redelivered OrderPlaced converges onto instead of double-counting.
    #
    # This duplicates `analytics.order_item_facts` deliberately. Analytics
    # owns the METRICS — the six FR-95 numbers, FR-97 conversion, FR-98's
    # restaurant view. This owns a FEATURE the assistant answers with, and
    # putting it behind a synchronous call would add a failure mode and a
    # round trip to every turn that needs it. One topic, two consumer
    # groups, two read models: the shape this system uses everywhere.
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("item_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("qty", sa.Integer, nullable=False),
    sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
# Popularity reads by restaurant and time (FR-80); taste profiles read by
# user and time (FR-75). Aggregates are computed at READ time from these
# rows and never materialised back as counters — a counter cannot absorb
# at-least-once redelivery, which is the whole reason these are facts.
sa.Index("ix_order_items_place_time", order_items.c.restaurant_id, order_items.c.placed_at)
sa.Index("ix_order_items_user_time", order_items.c.user_id, order_items.c.placed_at)


# One row per customer who has ordered. Built OFFLINE (FR-75) by a periodic
# task, so the panel-open path is a single-row lookup rather than an
# aggregation over a history — the read happens before a customer has typed
# anything, which is the least forgiving moment to spend a group-by.
#
# The cost is staleness, and it is bounded and visible: `built_at` says how
# old the opinion is, and a profile older than the builder's interval means
# the builder has stopped, not that the customer stopped eating.
taste_profiles = sa.Table(
    "taste_profiles",
    metadata,
    sa.Column("user_id", sa.Text, primary_key=True),
    # Counts, not normalised weights: the raw numbers are what makes a
    # profile legible to a human reading the table, and normalisation is a
    # scoring concern that belongs where scoring happens.
    sa.Column("cuisines", sa.JSON, nullable=False),
    sa.Column("tags", sa.JSON, nullable=False),
    sa.Column("restaurants", sa.JSON, nullable=False),
    # What they have already had — the novelty nudge needs it, and it is the
    # one part of a profile that must not be inferred from the counts.
    sa.Column("ordered", _slugs(), nullable=False),
    sa.Column("orders", sa.Integer, nullable=False),
    sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# Browsing, which FR-75 names alongside order history. Weaker evidence and
# deliberately used as such: a view says a customer looked at a RESTAURANT,
# not that they wanted a dish, so it feeds restaurant familiarity and
# nothing else.
menu_views = sa.Table(
    "menu_views",
    metadata,
    # uuid5(request_id), minted at the emitter — so at-least-once
    # redelivery collapses on this PK, the same natural-key dedupe the rest
    # of this schema uses, applied to telemetry.
    sa.Column("view_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("viewed_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_menu_views_user_time", menu_views.c.user_id, menu_views.c.viewed_at)


# ── B4: recommendation acceptance (FR-79) ───────────────────────────

recommendations_shown = sa.Table(
    "recommendations_shown",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("city", sa.Text, nullable=False),
    # Which surface put these dishes in front of the customer: the panel's
    # recommendation list, or the citations under a streamed answer. One
    # table for both, because "of the dishes we suggested, how many were
    # ordered" is one product question — splitting it by surface is a GROUP
    # BY, not a second schema.
    sa.Column("surface", sa.Text, nullable=False),
    sa.Column("basis", sa.Text, nullable=False, server_default=""),
    sa.Column("item_ids", _slugs(), nullable=False),
    sa.Column("shown_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_shown_user_time", recommendations_shown.c.user_id, recommendations_shown.c.shown_at)

recommendation_acceptances = sa.Table(
    "recommendation_acceptances",
    metadata,
    # The natural key, and the reason acceptance can be computed from an
    # at-least-once stream: one order can accept one showing exactly once.
    # A redelivered `OrderPlaced` loses this insert and stages no second
    # event (ADR-0035's rule, applied to a derived fact).
    sa.Column("shown_id", sa.Text, primary_key=True),
    sa.Column("order_id", sa.Text, primary_key=True),
    # The INTERSECTION — which suggested dishes were actually ordered. An
    # order that took one of five suggestions is not the same signal as one
    # that took all five, and a boolean would lose that.
    sa.Column("item_ids", _slugs(), nullable=False),
    sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=False),
)


# ── B6: the content studio (FR-88..FR-93) ───────────────────────────

DRAFT_KINDS: tuple[str, ...] = (
    "menu_item",
    "promotion",
    "engagement",
    # FR-92's summary rides the same table. It is generated text a human
    # reads, with the same queue, the same parking and the same replay —
    # a second table would duplicate all of it to hold one more shape.
    # It is never published; `approve` refuses this kind.
    "feedback_summary",
)
"""What a draft is copy FOR. Closed, because each kind is a different
prompt over a different input and a different publish path — an open set
would mean a generic "write something" job nobody can review."""

DRAFT_STATUSES: tuple[str, ...] = (
    "queued",
    "drafted",
    "parked",
    "published",
    "rejected",
)
"""The job's life, and the job's whole state. There is no second place to
look.

- `queued` — the row exists and a task is enqueued. The row is written
  FIRST and committed, so a worker that picks the job up always finds it
  (notification's receipts learned this the same way).
- `drafted` — a model wrote something; a human has not seen it.
- `parked` — generation failed in a way retrying cannot fix. This IS the
  dead-letter queue, and it is a row rather than a broker artifact for the
  reason UC-25 asks for: "parked and visible, replayable". A message in a
  broker DLQ is visible to an operator with a console; a row is visible to
  the restaurant whose copy never arrived, and replaying it is an UPDATE.
- `published` — approved by a human and written through the ordinary
  Catalog path (FR-93).
- `rejected` — a human said no. Retained, never deleted: FR-93 keeps
  rejected drafts for audit, which is also the only record of what the
  model proposed and a person declined.
"""

content_drafts = sa.Table(
    "content_drafts",
    metadata,
    sa.Column("draft_id", sa.Text, primary_key=True),
    # Claim-scoped on every read and write. A brand token may draft for any
    # of its branches (ADR-0028) and `restaurant_id` is the branch the copy
    # is FOR, so both are stored and both are checked.
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("kind", sa.Text, nullable=False),
    # The menu item a `menu_item` draft describes. Null for the kinds that
    # are about the restaurant rather than one dish.
    sa.Column("target_id", sa.Text, nullable=True),
    # What the admin asked for, verbatim and uninterpreted — "an offer for
    # slow Tuesdays". Model INPUT, so it is untrusted text by ADR-0043's
    # rule, defended where it is used rather than mangled here.
    sa.Column("request", sa.Text, nullable=True),
    # The facts this copy must be written FROM, frozen when the admin asked
    # (FR-88). Frozen rather than looked up by the worker for three
    # reasons: the worker then needs no cross-service call, the draft
    # describes what the admin was looking at rather than racing a later
    # edit, and the row records exactly what the model was told — which is
    # what makes a draft reviewable a week afterwards.
    sa.Column("subject", sa.JSON, nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default="queued"),
    # What the model wrote. NEVER written to `menu_items` from here: FR-88
    # is explicit that output lands in this table and nowhere else, which
    # is what makes "never a half-written menu" structural.
    sa.Column("content", sa.Text, nullable=True),
    # What the human actually published, which may not be what the model
    # wrote — FR-93 lets them edit before accepting. Keeping both is the
    # only way to ever answer "how much of this did people have to fix".
    sa.Column("published_content", sa.Text, nullable=True),
    sa.Column("model", sa.Text, nullable=True),
    # Why it parked, in words an operator can act on. The replay lever is
    # moving `parked` back to `queued`; this is how they decide whether to.
    sa.Column("error", sa.Text, nullable=True),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # Who accepted or declined it. FR-93 says no generated text reaches a
    # customer without an explicit approve action; this is the record of
    # whose action it was.
    sa.Column("decided_by", sa.Text, nullable=True),
    sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint(f"kind IN {DRAFT_KINDS!r}", name="ck_content_drafts_kind"),
    sa.CheckConstraint(f"status IN {DRAFT_STATUSES!r}", name="ck_content_drafts_status"),
)

# The console's read: one restaurant's drafts, newest first, usually
# filtered by status.
sa.Index(
    "ix_content_drafts_restaurant",
    content_drafts.c.restaurant_id,
    content_drafts.c.status,
    content_drafts.c.created_at,
)
