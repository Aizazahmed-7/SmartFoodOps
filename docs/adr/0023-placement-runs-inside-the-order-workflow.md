# 0023 — Placement runs inside the order workflow (update-with-start), sweeper retired

**Status**: Accepted 2026-08-18 (team review decision). Supersedes the
commit-then-start design that [placement-initiation-options.md](../placement-initiation-options.md)
recorded as option B; that document remains the full comparison and is now
the record of what was traded away.

## Context

`POST /v1/orders` used to commit the order, its line snapshots, the
`OrderPlaced` outbox row and the idempotency completion in one transaction,
then start `OrderWorkflow` post-commit. A crash in the commit→start gap left
a PLACED row with no saga, healed by a background sweeper.

Review asked the recurring question — why is placement not simply part of the
workflow? — and the answer this time was to move it, in the *hybrid* form:
the workflow owns the order's creation, but everything a customer can be told
synchronously stays in the request.

## Decision

**The saga creates the order.** `POST /v1/orders` still reserves the
idempotency key, resolves the address, snapshots the menu and prices the cart
in-process — so `PRICE_CHANGED`, `ITEM_UNAVAILABLE`, `RESTAURANT_CLOSED` and
unknown-address remain synchronous 4xx with the key released for a clean
retry. It then hands a fully-priced `PlacementInput` to Temporal and waits for
the order to exist.

- **One RPC starts and awaits**: `execute_update_with_start_workflow`
  (ExecuteMultiOperation) sends the start together with the `await_placement`
  update. The workflow's first activity, `create_order`, performs the same
  four writes as before — now in the worker — and the update resolves with
  `PlacementAck(order_id, status)` the moment it commits. Sending start and
  update separately would reopen the very gap this replaces.
- **`id_conflict_policy=USE_EXISTING`** (running → attach) and
  **`id_reuse_policy=REJECT_DUPLICATE`** (closed → refuse) answer different
  questions; both are pinned by test.
- **Order ids are derived, never random**:
  `ord_{uuid5(NS, "{scope}:{idem_key}")}`. With the order written by a worker,
  the window between reserving a key and completing it is long enough for the
  store's stale-`IN_PROGRESS` takeover (300s) to fire; a `uuid4()` there would
  mint a second id, a second `ord::` workflow and a second dinner. Derivation
  makes the retry converge on the existing row, and makes the `create_order`
  activity safely re-runnable (at-least-once: it may commit and then be
  retried).
- **No release after the start RPC is attempted.** Freeing the key when we
  cannot know whether Temporal accepted the start would let a retry fork a new
  id against a live workflow. The key stays `IN_PROGRESS`, so a retry either
  waits (409) or takes over onto the *same* id.
- **Timeout ≠ failure**: when the await budget (`placement_await_seconds`,
  default 2s) expires the workflow is still durable, so the route answers 202
  with the derived id (`PlacementPending`), never 5xx.
- **The placement key's takeover window drops to 30s** (library default is
  300s). That default was sized for "a crash mid-transaction — do not
  re-execute eagerly", a caution the derived order id removes: a takeover
  provably converges on the same order and the same workflow. Short matters
  because the key is *not* released on a Temporal outage, so 300s would lock
  a customer out of that exact cart for five minutes.
- **An `IdempotencyJanitor` reclaims the table** — COMPLETE rows past the 24h
  replay TTL and IN_PROGRESS rows abandoned past an hour. Nothing collected
  them before; every other row in the system has an owner (outbox partitions,
  the reservation reaper, Temporal retention) and these had none.
- **The sweeper is deleted** — with its `ix_orders_sweeper` partial index
  (migration 0003), config keys, port method and tests. An order cannot exist
  without a saga now, because the saga is what makes it.
- **`price_order` is deleted too**: the numbers travel in the workflow input,
  so the saga no longer reads back what it was just told. Placement moved in
  without growing the action budget.

## Consequences

**Positive**
- No commit→start gap and no reconciliation loop: Temporal holds the intent
  from the first RPC.
- One owner for the order's whole life; the Temporal UI shows placement.
- Client retries are refereed by Temporal (attach) *and* the derived id
  (same row), instead of by a background scan.

**Negative — the accepted costs**
- **Temporal is now a checkout dependency.** Its outage is a 503 on
  placement, where it used to be a 202 plus a delayed saga. This is the
  single biggest change and the one to revisit first.
- **Worker schedule-to-start latency is inside NFR-2's p95 < 3s**, and a
  rolling worker deploy shows up as checkout latency.
- **Read-your-writes is briefly suspended on the pending path** — `GET
  /v1/orders/{id}` may 404 for a moment (the FE polls through it), and
  `409 IDEMPOTENCY_IN_PROGRESS` becomes a routine client state rather than a
  corner case (the client waits it out on the same key).
- **`card_token` now lives in workflow history** (it must cross Temporal to
  reach the database). Acceptable against the mock PSP; a payload codec is
  required before a real one.

**Revisit trigger**: placement 5xx attributable to Temporal exceeding the
error budget, or p95 placement latency breaching NFR-2 because of
schedule-to-start — either would argue for going back to committing first and
re-introducing a healer.
