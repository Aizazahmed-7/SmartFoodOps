# 0046 — Order milestones are facts about moments

**Status**: Accepted (2026-09-28)

## Context

Part B's explanation engine (FR-82/83) answers "what is happening with my
order?" from facts, not from a model's guess. The first requirement is
therefore banal and load-bearing: the system has to KNOW what happened and
when. It currently does not.

Two Part A decisions, each correct on its own terms, combine to erase the
timeline:

1. **`orders.updated_at` is overwritten by every move.** The row can say
   when something last changed, never what took long. An order that sat 40
   minutes in a kitchen and one accepted 40 minutes ago and instantly cooked
   are the same row.

2. **The topic carries major states only** — `OrderPlaced`, `OrderConfirmed`,
   `OrderCancelled`, `OrderDelivered`, `OrderSettled`. The vocabulary says
   why: "no per-transition spam". So `ACCEPTED`, `PREPARING`, `READY` and
   `PICKED_UP` happen silently. The four stages where delay actually occurs
   are exactly the four nobody can observe.

An explanation engine over this can only say "your order is PREPARING",
which the customer can already see, or invent a duration — which is the
specific failure ADR-0043 exists to prevent.

## Decision

1. **Nullable milestone columns on `orders`** — `confirmed_at`,
   `accepted_at`, `preparing_at`, `ready_at`, `picked_up_at` — stamped by
   `transition()`, **inside the guarded UPDATE** and nowhere else.

   The guard is the whole argument. `WHERE status = :expected` means a
   replayed or raced transition matches zero rows, so a redelivery arriving
   minutes later cannot overwrite a moment that already passed. A milestone
   is a fact about when something happened; the only code permitted to write
   it is the code that caused it to happen. This is ADR-0035's at-least-once
   rule paying for itself a second time: the same guard that makes replays
   safe makes timestamps trustworthy, for free.

2. **Four new event types** on `c1.orders.events` — `OrderAccepted`,
   `OrderPreparing`, `OrderReady`, `OrderPickedUp`.

   This amends Part A's stated contract, so it is worth being precise about
   what the contract was protecting. "No per-transition spam" is a statement
   about *notifications*, and the anti-spam intent lives in notification's
   `order_drafts`, not in the vocabulary — that function returns no draft
   for any of the four, so no customer gains a push. A test pins it, so that
   "your food is being prepared" remains a product decision someone makes on
   purpose rather than a side effect of an event coming into existence.

3. **Every milestone event carries the whole timeline**, not just its own
   moment. The payload gains `milestones` and `placed_at` alongside the
   existing full state. A consumer joining the stream at `OrderReady` can
   time the cooking without replaying the topic from its beginning — which
   is what "full-state events" claimed all along.

4. **`confirmed_at` was added second, and the reason is worth recording**
   (amendment, 2026-09-29). The first cut took FR-81's four stages
   literally. FR-84 then asked for a stage-based ETA whose earliest basis
   is `accept_timeout_s` — a timer that runs from CONFIRMED. With only the
   four, the resolver would have had to time the restaurant's decision
   window from `placed_at`, silently charging the restaurant for the saga's
   reserve-and-authorize round trip. A budget is only as honest as the
   clock it starts from.

5. **Terminal moves get no column.** `DELIVERED`, `SETTLED` and `CANCELLED`
   already announce themselves with `occurred_at` in the payload, and
   analytics already folds them into per-order fact rows. Adding columns for
   them would duplicate a record that exists and is already correct.

## Consequences

**The engine can be truthful.** "Accepted 4 minutes ago, still not started"
is a fact with a clock behind it. Every branch of FR-82's resolver reads
these columns; none of them needs a model.

**Rows born before this migration have no history, and that is honest.**
The columns are nullable and are NOT backfilled — an order that died at
`VALIDATED` never had a `ready_at`, and inventing one from `updated_at`
would produce a timeline that looks precise and is fiction. NULL means "not
reached, or not recorded", and the resolver treats both the same way,
because to a customer asking what is happening now, they are the same.

**Analytics deliberately ignores the four.** Its `event_values` returns
`None` for unknown types "for forward compatibility", so a widened producer
cannot park its batches — that is the property being relied on. Its fact
table times an order end to end; stage-level congestion is the explanation
engine's job, read from Order's own row. If analytics ever wants the stages,
it is four lines in `_EVENT_COLUMNS` and a migration, not a redesign.

**A post-hoc "why was my delivery slow" is only reconstructible to
`picked_up_at`.** The road segment is bounded by the `OrderDelivered` event
rather than a column. That is enough for FR-83, which explains orders in
flight, and it is the limit to revisit if FR-84's ETA reasoning later wants
to learn from completed deliveries.
