# 0027 — Menu cache is cache-aside; the versioned blob + pointer scheme is retired

**Status**: Accepted (2026-08-27)

## Context

The rendered-menu cache shipped as a two-key scheme: an immutable blob per
menu version (`catalog:menu:{rid}:<ver>`, 24h) and a tiny pointer to the
current version (`catalog:menu:ptr:{rid}`, 7d). Writes deleted only the
pointer; readers resolved pointer → blob; a version-addressed read
(`GET /v1/menus/{rid}?v=N`) served old versions straight from cache, and the
deployment design leaned on that for forever-cacheable CDN URLs.

That scheme bought three things: a reader that raced an edit could only
re-install a *truthfully labeled* old blob (never a lie under the key readers
consult), old versions stayed servable for pinned carts and the CDN, and
stale entries became unreachable garbage rather than served content.

It also cost things: two Redis GETs per hit, dead blobs held for 24h, more
code, and a second key scheme to explain. Nothing in the product exercised
the extra power — the FE never requested `?v=N`, carts re-fetch the current
menu, and checkout prices from the snapshot endpoint, which **bypasses every
cache by design**. The protection that actually matters at the money path
never depended on the cache at all.

## Decision

The menu cache is plain **cache-aside** with one mutable key per restaurant:

- `catalog:menu:{rid}` — the rendered current menu, filled on read miss.
- Every menu mutation ends with `DEL catalog:menu:{rid}` after commit.
- **TTL 5 minutes** (`MENU_TTL_SECONDS`), not 24h: cache-aside accepts one
  race — a reader that loaded PG just before an edit can SET a just-stale
  menu right after the edit's DEL, and nothing corrects that entry. The TTL
  is the ceiling on how long that lie lives. Short TTL is the price of the
  simpler scheme; it is deliberate, not tunable-upward-for-hit-rate.
- The singleflight render lock (`catalog:lock:menu:{rid}`, 3s) is unchanged —
  stampede control is orthogonal to key layout. So is the torn-read version
  re-check (`_consistent_read`): a doc must never advertise a version its
  rows don't match.
- `GET /v1/menus/{rid}?v=N`, `StaleMenuVersion`, and the immutable
  `Cache-Control` branch are deleted. The menu URL is near-fresh everywhere
  (`public, max-age=5`); the CDN needs no purge because nothing long-caches
  it.

## Consequences

- One GET per hit, one key scheme, ~40 fewer lines; Redis holds only hot
  menus (5-min TTL) instead of a day of blobs.
- Display staleness is now bounded by TTL instead of structurally impossible.
  Money is unaffected: pricing reads PG via the cache-bypassing snapshot.
- Old menu versions are no longer servable from cache. The deployment-era
  "version-in-path, cache forever" CDN design (ARCHITECTURE §10 row 3, old
  form) is retired with it; if a CDN-immutable menu URL is ever wanted
  again, this ADR is the one to supersede.
- ADR-0017's cart-staleness note ("re-fetches the version-addressed menu")
  now reads as a plain re-fetch of the current menu; the placement-time
  `PRICE_CHANGED` re-confirm it describes is unchanged and remains the
  authoritative guard.
