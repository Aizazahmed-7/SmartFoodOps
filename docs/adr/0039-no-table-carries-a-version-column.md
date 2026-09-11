# 0039 — No table carries a version column

**Status**: Accepted (2026-09-11) — completes the removal begun in
[0035](0035-random-event-ids.md), [0036](0036-placement-consents-to-a-total.md),
[0037](0037-one-snapshot-instead-of-a-version-recheck.md) and
[0038](0038-outbox-carries-no-aggregate-version.md).

## Context

Seven version columns survived across five databases:
`restaurants.version`, `orders.aggregate_version`, `payments.version`,
`stock.version`, `reservations.version`, `restaurant_load.version`,
`order_facts.aggregate_version`.

Each had been introduced as an optimistic-concurrency or ordering device.
By this point every one of those jobs had been taken over by something
else, and the four earlier ADRs removed the last *readers*. What remained
was seven counters incremented on every write and consulted by nothing.

The check that settled it: **not one version appears in a guard**. Every
guarded write in the system keys on the state it actually protects.

| Write | What the WHERE clause actually tests |
|---|---|
| `transition()` (orders) | `status = :expected` |
| `begin_cancel_from()` | `status IN :allowed` |
| `transition_payment()` | `status = :expected` |
| `decrement_stock()` | `available >= :qty` |
| `occupy_slot()` | `active < capacity` |
| `free_slot()` | `active > 0` |
| `finish_reservation()` | `status = 'active'` |
| `insert_stock` / `insert_load` | the composite primary key |

A version appearing in a `SET` clause next to one of those guards reads like
optimistic concurrency and is not: the row is already protected, and the
counter is decoration.

## Decision

1. All seven columns are dropped (five migrations).
2. `bump_version` becomes **`touch`**: the `updated_at` stamp was the half of
   it that was doing work, and an audit timestamp is worth keeping.
3. `TransitionResult.version` and `PaymentRepo.transition_payment`'s `int |
   None` return become `applied: bool` / `bool`. Both callers only ever
   asked whether the write landed.
4. `Restaurant.version` and `StockRow.version` leave the domain models;
   `StockAdjusted`'s payload loses its `version` field (it has no
   consumers at all).
5. Tests that asserted a bump now assert `updated_at`. That is a **stronger**
   statement, not a weaker one: "a replay did not bump the counter" becomes
   "a replay wrote nothing", which is the property the guard is for.

## Consequences

### Positive

- `restaurants` is now exactly what the ERD specifies:
  `id, owner_user_id, name, kind, created_at, updated_at`.
- Every write path's protection is legible at the WHERE clause, with no
  decorative counter beside it to suggest a second mechanism exists.
- Five schemas, two domain models, two API response shapes and one event
  payload get smaller.

### Negative

- **Optimistic concurrency is no longer available for free.** A future
  read-modify-write that genuinely needs it — one where the guard cannot be
  expressed against the state itself — must add a column back, and add it
  as a real compare-and-swap (`WHERE version = :seen`), not a counter in a
  `SET` list. The absence of any version column is the signal that no such
  write exists today.
- `order_facts.aggregate_version` is gone from analytics, so a future
  projector that wants version-guarded upserts must reintroduce the value
  through the payload (see ADR-0038's revisit trigger).
- Downgrades restore the columns at `0` for historical rows. The per-row
  sequences are not reconstructible, and an invented one would read as real.

## Revisit trigger

Any write that must detect "the row changed under me" and *cannot* say so
in terms of the row's own state. Add the column to that one table, use it
as a CAS in the WHERE clause, and say in its comment what concurrent writer
it is defending against — the thing none of these seven could say.
