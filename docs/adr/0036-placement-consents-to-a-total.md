# 0036 — Placement consents to a total, not a menu version

**Status**: Accepted (2026-09-11) — replaces the `menu_version` pin in the
placement contract. Falsifies the `menu_version`-pinning consequence recorded
in [ADR-0028](0028-brands-and-branch-menu-inheritance.md) §consequences and
the "menu version browsed" clause of
[ADR-0017](0017-cart-is-client-side.md). Prerequisite for removing the
`version` columns.

## Context

`POST /v1/orders` carried `menu_version`, and the pricing engine compared it
to `restaurants.version` before doing anything else:

```python
if expected_menu_version is not None and expected_menu_version != restaurant["version"]:
    raise MenuVersionChanged(current=restaurant["version"])
```

`restaurants.version` is bumped by **every** catalog mutation — a category
rename, a new item, a price change on an unrelated dish, and (since ADR-0028)
every base-menu edit fanned out to every branch. So the guard rejected every
in-flight cart on any edit anywhere in the restaurant, and answered
"re-confirm the new total" when the total had not moved.

The customer-facing error code was **already** `PRICE_CHANGED`. The mechanism
had drifted from the thing it was named for.

The frontend paid for the mismatch too. `useQuote` existed largely to keep a
pinned version in sync across two pages, and its own comment records the bug
that motivated it: *"When only Cart re-pinned, Checkout could display a fresh
price while placing with a stale pinned version — a guaranteed spurious
PRICE_CHANGED consent loop."*

## Decision

1. The placement body carries **`expected_total_cents`**: the total the
   customer was shown and is consenting to. `menu_version` is gone from the
   request.
2. It is **consent, not an assertion**. The server reprices from its own
   snapshot and compares; a client sending a favourable number gets a 409,
   never a discount. This preserves the api-standards §3 rule that a client
   never sends prices.
3. `MenuVersionChanged` becomes **`PriceChanged`**, carrying the recomputed
   total. The wire code stays `409 PRICE_CHANGED` — unchanged for clients.
4. The check moves to **after** pricing, because a total does not exist until
   it is computed. Consequence: `ItemUnavailable` and `RestaurantClosed` now
   outrank a price change when both apply.
5. The frontend stops pinning anything. Checkout sends
   `quote.data.totals.total_cents` — the same object it renders — so the
   price shown and the price consented to are one value. `cart.menuVersion`,
   `setMenuVersion`, and the re-pin `useEffect` are deleted, and `cart.add`
   no longer takes the restaurant's version.
6. The canary and both demo scripts quote before placing. That is the real
   client sequence, and a probe that skipped it would not catch a break in
   it.

## Consequences

### Positive

- The guard now fires on the thing it is named for. An unrelated menu edit
  leaves a cart placeable; the carted item repricing still refuses.
- The frontend's shown-price/consented-price divergence becomes
  unrepresentable rather than centrally managed. A whole piece of persisted
  cart state and its synchronising effect are gone.
- Removes the last functional reader of `restaurants.version` outside
  catalog, which is what the version-column removal was waiting on.

### Negative

- **A price change that nets to zero is no longer caught.** If one line
  rises by 100 and another falls by 100, the total matches and the order
  places at the old total with a changed composition. The version guard
  would have refused. Judged acceptable: the customer is charged exactly
  what they consented to, the per-line snapshot in `order_items` records
  what they actually got, and no money is lost in either direction.
- Error precedence changed (decision 4). A client that special-cased
  `PRICE_CHANGED` ahead of `ITEM_UNAVAILABLE` will now see the latter first.
  Both are 409s the same re-quote flow handles.
- The comparison is a full repricing rather than an integer compare, so a
  refusal costs the work of pricing. Placement already priced
  unconditionally, so this is free in practice.

### Owed, not done

- `docs/flows.md` §placement still draws `menu_version 7` in the sequence
  and the brands note still explains version pinning. Updated with this ADR.
- `restaurants.version` remains for catalog's own torn-read guard, cache
  keying and the convergence marker; those come out in the following steps.

## Revisit trigger

If the zero-net composition change above ever matters — a promo engine that
can move two lines in opposite directions, say — compare a hash of the
priced line set instead of the scalar total. That keeps consent client-side
and does not reintroduce a version column.
