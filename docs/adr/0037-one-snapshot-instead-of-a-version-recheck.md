# 0037 — Consistent reads come from one snapshot, not a version re-check

**Status**: Accepted (2026-09-11) — removes the last two functional readers of
`restaurants.version`, which [ADR-0027](0027-menu-cache-aside.md) left in place
when it retired the versioned blob scheme.

## Context

Catalog's two multi-query reads — the menu render (~9 queries) and
`pricing_read` (4) — ran under READ COMMITTED, so they could straddle a
commit and build a document mixing two menus. Both defended with the same
loop:

```python
for _ in range(2):  # bounded retries
    check = await repo.get_restaurant(restaurant_id)
    if check is None or check.version == restaurant.version:
        break
    ...re-read everything...
```

Two problems. It **detects** tearing after the fact rather than preventing
it, and it gives up after three attempts — the docstring said the quiet part
out loud: *"after 3 tries serve the last read (staleness is display-only; a
mixed-version doc is not)"*. On the menu render that is arguably fine. On
`pricing_read` it is the money path: a torn read prices one line from the old
menu and another from the new, and the customer is charged a total that never
existed on any single menu.

Separately, `get_unpublished_brands` used `version == 0` to mean "this brand
has never published its cutover storm".

## Decision

1. `CatalogRepo.begin_snapshot()` sets **REPEATABLE READ** on the
   transaction, and both read paths call it as their first statement. Every
   query in the read then comes from one snapshot; the retry loops are
   deleted.
2. It is a **no-op on sqlite**, which has no equivalent level and needs
   none — the unit suite drives one connection through `StaticPool`, so its
   reads cannot interleave with another writer.
3. The isolation level **must** be set before any statement: Postgres
   cannot change it once the transaction has issued one. A test asserts the
   call ordering on both paths, because a query added above it would
   silently downgrade the read and nothing else would notice.
4. `get_unpublished_brands` asks the **outbox** instead:
   `kind = 'brand' AND NOT EXISTS (outbox row for this id)`. Exactly
   equivalent — `_stage_one` bumps the version and stages the event in one
   transaction, so "never bumped" and "has no outbox row" were always the
   same set. No new column: the ERD's target `restaurants` is
   `id, owner_user_id, name, kind, created_at, updated_at`, and a
   `converged_at` marker would have deviated from it to store a fact the
   outbox already holds.

## Consequences

### Positive

- Tearing becomes unrepresentable rather than probabilistically reduced. The
  money path no longer has a documented "give up and serve it anyway" case.
- Removes the last two functional readers of `restaurants.version`.
- Read-only REPEATABLE READ transactions raise no serialization failures,
  so there is no new retry burden — verified on PG 15.

### Negative

- A long-running snapshot holds an older xmin, which delays vacuum for its
  duration. These reads are single-digit milliseconds, so the exposure is
  the same as any short transaction — but a future long-running read added
  to these paths would now have a cost it did not have before.
- `begin_snapshot()`'s placement is load-bearing and cannot be enforced by
  the type system. The ordering tests are the guard.
- The convergence query is a `NOT EXISTS` against an unindexed
  `outbox.aggregate_id`. It runs once per boot over brand rows only, so no
  index was added; a hot-path caller would need one.

## Verification

Not provable in the unit suite (sqlite has no REPEATABLE READ). Run against
Postgres 15 with a real concurrent committing writer interleaved between two
reads of one transaction:

- **control**, no snapshot: second read returns the new price — so the test
  can detect a regression
- **with the snapshot**: both reads return the old price
- the writer's commit is real and visible to the *next* transaction
- the read-only snapshot commits without a 40001

And for the marker: both minted brands pending, branches excluded, one
publishes → only the survivor remains pending, and a staged-but-undrained
row already counts as converged (staging is the commit, not the drain).

## Revisit trigger

If a read path here ever needs to span more than a few milliseconds — a
paginated export, say — reconsider, because the vacuum cost above scales
with snapshot lifetime. A cursor-per-page under separate short snapshots
would be the fix, not a return to version comparison.
