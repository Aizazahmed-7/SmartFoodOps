# 0024 — The orders row is placement's idempotency record; the key table retired

**Status**: Accepted 2026-08-20 (design review — raised by the team against
ADR-0023's own mechanics). Amends ADR-0023: the update-with-start placement
and the derived order id stay; the `idempotency_keys` table, its takeover
protocol, and its janitor go. Payment's money-key table is explicitly out of
scope and stays (`smartfood-idempotency` lives on there).

## Context

ADR-0023 derived the order id from `(user, Idempotency-Key)` — and that one
property quietly made the placement key table redundant. The table's
remaining jobs each had a simpler owner:

- *"What did we already answer?"* — the orders row, read by derived id.
- *"Serialize concurrent duplicates"* — Temporal: same workflow id,
  `USE_EXISTING` attaches, both callers get the same ack.
- *"Same key, different body"* — one `request_hash` column on the row.

Meanwhile the table's IN_PROGRESS lock had become self-serving complexity:
locks whose holders die need a takeover protocol (300s→30s tuning debates),
and rows nobody retries need a janitor. Delete the lock and both problems
evaporate rather than get solved.

## Decision

**`idempotency_keys` is dropped from order_db** (migration 0004). Placement
idempotency becomes three mechanisms, all pre-existing:

1. **Read the row first.** `place()` derives the order id and reads the
   orders row *before pricing*: hash matches (or predates the column, NULL)
   → 202 replay with the row's **current** status + `Idempotent-Replay:
   true`; hash differs → **422 IDEMPOTENCY_KEY_REUSE** (`request_hash`, new
   nullable column, stamped by `create_order`). Reading before pricing is
   what keeps a replay immune to menu drift — an existing order is never
   re-priced into a 409.
2. **Temporal referees concurrency.** Two simultaneous submits with one key
   derive one workflow id; one starts, the other attaches, both block on
   `await_placement` and receive the same ack. Strictly better than the old
   409-and-poll: the loser now gets the real answer.
3. **The pending-window carve-out** (the one race this design widened,
   closed properly): a retry that lands while the workflow is in flight but
   the row is not yet visible re-prices — and if the menu drifted, refuses.
   That refusal must LOSE to the running workflow: on a deterministic
   `PricingError`, `place()` probes `attach_placement(order_id)`
   (update-only, no start). Running → its ack outranks the 409; `SagaGone`
   (or Temporal unreachable) → the refusal stands. Without this, "re-confirm
   your cart" mints a second order for one dinner.

**What a failure leaves behind: nothing.** A Temporal outage during
placement writes no row and no lock — the retry re-derives the id and simply
runs again. No takeover window, no locked-out cart, no orphan to collect.

**Deleted with the table**: `IdempotencyStore` usage in order (reserve /
complete / release), the 30s takeover setting, the janitor wiring,
`scope`/`idem_key` from `PlacementInput` (`create_order` is three writes),
`409 IDEMPOTENCY_IN_PROGRESS` from placement's vocabulary, and the FE's
409-wait loop. The `Idempotency-Key` header stays **required** — it is the
derivation seed; only the server-side ledger of it is gone.

## Consequences

**Positive**
- One fewer table, no lock lifecycle, no GC; the placement path is
  read-row → price → update-with-start, with every retry shape converging
  on one order by construction.
- Replays report the order's *current* status — truer than the frozen
  202 body the table replayed.
- Mid-flight retries get answers (attach) instead of 409-wait-loops.

**Negative / accepted**
- Concurrent duplicates each run the identity+catalog+pricing reads before
  Temporal collapses them — wasted reads, no correctness cost.
- Replay is no longer byte-identical to the first response (status may have
  advanced). api-standards §4's placement row is rewritten accordingly.
- The attach probe adds one Temporal RPC on the rare PricingError path.
- Old rows (request_hash NULL) skip the body guard forever — accepted;
  their client keys were cleared on success anyway.

**Revisit trigger**: a placement-shaped endpoint where the response is NOT
reconstructible from owned rows (payment's stored 402 is exactly that —
which is why its table stays).
