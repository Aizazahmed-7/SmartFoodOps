# 0028 — Brands as restaurant rows; branch menus inherit by fan-out

**Status**: Accepted (2026-08-27)

## Context

Owners need multiple locations sharing one menu. Product decisions (user):
branches share a BASE menu and may add local items and locally 86 a base
item (no per-branch price overrides); a brand entity owns the base menu;
one owner account runs the brand and every branch (the JWT claim becomes
brand-scoped); customers see branches as separate browse cards.

The blockers found by exploration: `UNIQUE(owner_user_id)` on restaurants,
identity's single `restaurant_id` claim (a second grant was a 409), the
claim-scoped kitchen/notification/analytics surfaces — and inventory's
stock PK of `item_id` alone, which silently swallows the second branch's
row for a shared base item.

## Decision

1. **A brand is a row in the existing `restaurants` table** (`kind`
   brand|branch; branches carry `brand_id` + `branch_label`; partial unique
   keeps one BRAND per owner). Menu tables FK restaurants.id, so the base
   menu attaches to the brand row with zero menu-schema changes. Brand rows
   never appear in browse/search.
2. **The cutover minted NEW brand rows; legacy restaurants stayed branches**
   (ids unchanged — stock rows, dispatch pins, deep links, active orders all
   undisturbed). Migration 0007 re-pointed menu rows to the brand (item ids
   unchanged) and labeled each legacy branch "Main".
3. **The claim carries the BRAND id** (wire name `X-Auth-Restaurant-Id`
   unchanged). `grant_restaurant_admin` repoints last-writer-wins (callers
   are SystemOnly; catalog enforces one brand per owner), so the EXISTING
   grant-convergence consumer moved every owner automatically. Ownership
   checks became equality-or-parentage; order/notification/analytics scope
   by a `brand_id` carried in events (kitchen feed: `brand_id = :claim OR
   restaurant_id = :claim` — old tokens and future branch-scoped managers
   ride the second arm).
4. **Base edits fan out**: one catalog transaction bumps EVERY branch's
   version and stages one full-EFFECTIVE-state event per branch plus the
   brand's own event — all-or-nothing. Consequences: `menu_version` pinning
   (`MenuVersionChanged`) needed zero changes, cache invalidation stays the
   per-key delete, `_consistent_read` stays sound, and inventory
   provisioning consumes per-branch effective payloads with no consumer
   changes. Bounded by `MAX_BRANCHES = 20` (~8 statements/branch, one
   commit).
5. **No manual backfills.** The boot-time `converge_brand_events()`
   publishes every version-0 brand once (crash-resumable); the compacted
   topic then durably carries `brand_id` per branch key, and small
   naturally-idempotent consumers in order and analytics heal legacy rows
   (`… SET brand_id WHERE restaurant_id = :aggregate AND brand_id IS NULL`).
   Fresh environments and replays self-converge.
6. **Branch-86 is presence-only** (`branch_item_overrides(branch_id,
   item_id)`): a row means "not serving here"; restore = DELETE. Price
   overrides stay unrepresentable by construction. Render and the pricing
   snapshot read base ∪ local minus overrides; items carry `source`
   (base|local) for the dashboard.
7. **Inventory stock PK → (restaurant_id, item_id)**: each branch's fridge
   counts its shared base items independently. `StockScopeMismatch` became
   unrepresentable and was retired; `STOCK_ADJUSTED` aggregates re-keyed to
   `"{restaurant_id}:{item_id}"` (no consumers existed). Ownership checks
   resolve branch→brand via a memoized catalog lookup (sync on purpose —
   the seed stocks items immediately after creating them; an event-fed map
   would race); catalog-down on a needed lookup is a truthful 503.
8. **`name` stays the pure brand name** (copied to branch rows, re-copied
   on brand rename in the same tx); `display_name` composes
   "Biryani House — Downtown". Demo scripts' find-by-name kept working
   unmodified.

## Consequences

- The repoint window (≤15-min tokens) is covered by the OR arms and the
  `_own` equality arm; a notification draft for a legacy in-flight order may
  address the branch mailbox until the order's next event carries the brand.
- A compacted-topic replay of a pre-cutover event can transiently flap a
  grant; per-partition ordering plus the storm's newer event heals it in
  the same poll.
- The cutover's version bumps 409 any pinned cart once (PRICE_CHANGED
  re-confirm) — by design.
- Deferred: per-branch manager accounts (branch claims already pass every
  arm), a brand page/branch picker, brand-wide pause, per-branch analytics
  breakdown, search results filtered by per-branch 86 (display-only).
- ADR-0017's cart note and the demo scripts read a branch card exactly as
  they read a restaurant before — the word "restaurant" in older ADRs now
  means "branch" wherever a location is implied and "brand" where menu
  ownership is implied.
