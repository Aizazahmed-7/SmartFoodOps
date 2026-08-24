# W3 Fork A — overnight build walkthrough (2026-08-24)

Written slice-by-slice as the work landed, for the morning teaching session.
Each section: what was built, the design decisions and WHY, what "Uber-Eats
traffic" changes about the design, and how it was verified.

## S1 — Opening hours (committed f59e1be)

**Problem:** `restaurants.hours` was stored and echoed but never consulted —
a 4am order against an 11:00–23:00 schedule succeeded.

**Design:**
- New pure function `smartfood_pricing.is_open_at(hours, tz, now)` — takes
  `now` as an argument (no clock inside), which is why overnight windows,
  DST, and split days are all plain unit tests (19 of them).
- Catalog owns the clock: it computes `open_now` into the pricing snapshot;
  the engine just reads a boolean. The engine stays a pure function of its
  snapshot — the property that lets quote and placement share one pricer.
- `timezone` column (IANA, validated against the OS tz database at the API
  boundary), config-driven default, backfilled by `server_default`.
- Two separate "closed" notions kept apart: `status` = owner paused,
  `open_now` = outside posted hours. A merged flag would lie to customers.
- Malformed hours / unknown zone **degrade OPEN** — same house contract as
  the cache adapter ("loss degrades, never corrupts"): a parse bug must not
  silently cost an owner their dinner rush.
- Boundary semantics: `[open, close)` — inclusive open, exclusive close —
  documented in the module; makes back-to-back windows continuous.

**Scale note:** `open_now` is computed per snapshot read, which is cached at
catalog's pointer-cache layer; at Uber volume the snapshot TTL bounds the
staleness of the open/closed answer (seconds), which is acceptable — hours
change rarely and pause/resume (instant) covers emergencies.

**Verified:** 409 RESTAURANT_CLOSED on quote AND placement at Sun 19:25
against a 03:00–04:00 window; reopens when widened; migration 0004 applied
with every seeded row backfilled; 638 tests, 100% cov.

## S2 — Edge rate limiting (fixed window in Redis, fail-open)

**Where:** `edge_bff/limiter.py` + a check in `proxy()` after authentication.

**The three decisions:**
1. **Fixed window, one atomic Lua call** (`INCR` + first-hit `EXPIRE`) — one
   Redis round trip per request, no immortal keys on crash. Known artifact:
   a boundary-straddling burst can pass ~2x briefly; acceptable at the edge
   (a capacity shield, not a billing meter). Scale path: swap sliding-window/
   token-bucket behind the same interface.
2. **Scope = verified identity when authed (`sub:{user}`), first X-Forwarded-For
   hop when anonymous.** A NAT full of customers must not share a bucket.
   The XFF hop is trustworthy ONLY because the gateway sets it — a deploy-
   topology invariant recorded in the runbook. (Caught during build: reading
   the sub from the stamped-headers dict failed silently on header casing and
   merged every authed user into one bucket — scope now comes from claims.)
3. **Redis down = requests flow** (fail-open), matching the house "loss
   degrades, never corrupts, never 5xxes" contract. Failures are counted
   (`rate_limit_errors_total`) so a fail-open storm is visible.

Three budgets by route CLASS (flat label cardinality): auth 30/min (guessing
is the attack), read 300/min, write 120/min — config knobs, sized far above
the demo scripts. Empty `REDIS_URL` = disarmed (the otlp_endpoint idiom).
Edge uses Redis **db 1** — catalog's cache owns db 0, so flushing one never
clears the other.

**Obs parity:** `rate_limited_total{route_class}` counter, dashboard panel 14,
`RateLimitSpike` alert + runbook (12/12/12 parity holds).

**Verified live:** 310-request hammer → exactly 300×200 then 10×429 with
Retry-After + RateLimit-Limit/Remaining/Reset; `docker stop redis` → 5/5
requests still 200 with `rate_limit_errors_total` = 5; redis back → limiting
resumes. 648 tests, 100% cov, lint clean.
