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

## S3 — Analytics service (FR-43 + FR-55)

**Shape:** a new `services/analytics` mirroring notification (consumer-fed,
PG-backed, read API) — but a PROJECTOR, not an inbox.

**The central design argument — facts, not counters.** The obvious schema
(a counters table incremented per event) cannot survive at-least-once
delivery: `orders = orders + 1` applied twice is a lie, and no natural key
saves an increment. Instead ONE `order_facts` row per order holds absolute
values (status, total, one timestamp per milestone), upserted by order_id —
so a redelivered batch CONVERGES instead of double-counting. Aggregates
(daily rollups, rates, peak hour) are computed at read time from facts;
materializing them is the named scale knob, done then as periodic
recomputation, never as increments.

**Micro-batching (FR-43's "5s micro-batch") went into the LIB, done right:**
`EventConsumer.run_batches()` — getmany up to 500 events / 5s, ONE handler
call, ONE transaction, ONE offset commit. Failure containment in layers:
undecodable bytes park during assembly; a failing batch DEGRADES TO SINGLES
so one poison event costs its own DLQ slot, not the batch's progress;
nothing commits until the batch is accounted for. Stated trade-off: batch
mode drops per-message CONSUMER spans.

**One consumer loop, not two** (deviation from the plan's two): every one of
the eight buildable metrics derives from order events alone — payments
events add nothing. Less to operate; documented.

**Two deliberate nulls in the API:** `rider_utilization: null` (blocked on
dispatch — named, not faked) and rates answer `null` on an empty window
("no data" ≠ "perfectly zero"). FR-55's view scopes by the CLAIM — there is
no /{restaurant_id} to probe, so cross-tenant reads are unrepresentable.

**Two real bugs the live stack caught (the whole point of verifying live):**
1. The Avro envelope's `payload` is a JSON STRING; I indexed it like a dict.
   The batch runtime did exactly its job: degraded to singles, parked every
   event with forensics. Fixed, then `make dlq-replay` healed the parked
   history — the tooling recovering from the bug that its own tests missed.
2. Two producer shapes share the topic: transition events stamp
   `occurred_at`, create_order's OrderPlaced stamps `placed_at`. History is
   immutable, so the READER tolerates both.

Also fixed while wiring: `up-obs` now converges initdb like up-m3 (else the
new database only appears via up-m3), and the app-env anchor gained
ANALYTICS_BASE_URL (the edge was resolving its localhost default inside the
container — DEPENDENCY_UNAVAILABLE until then).

**Verified live:** 654 historical orders folded from the topic (the canary's
days of place-and-cancel — cancellation_rate 0.9848, exactly what the canary
does); avg_delivery_seconds 53.4 (= the simulated courier's 20+30s timers);
owner view through gateway→edge→analytics shows daily revenue 12288¢ =
3 settled × 4096¢. 676 tests, 100% cov.

## S4 — SSE live tracking (FR-36 transport, FR-38 auth)

**The auth design (FR-38), and why it is shaped this way:** EventSource
cannot send an Authorization header, and a JWT in a query string soaks into
access logs and referrers. So the edge-authed `POST /v1/track/ticket`
(ownership-checked, not-yours → 404) sells a 60-second single-use ticket;
the stream endpoint redeems it with Redis **GETDEL** — atomic
read-and-destroy, so replay is structurally impossible rather than merely
forbidden. A mismatched ticket is burned too: a probe learns nothing and
loses its ticket trying. Verified live: second use → 401.

**The topology:** `/sse/track/*` goes gateway → order DIRECTLY, bypassing
the edge — that is the point of the ticket: the stream fleet never touches
JWTs/JWKS. The nginx location that held the phase-2 "tracking-gateway lands
later" 503 is now the real thing, so when the dedicated gateway arrives it
mounts at the same URL and the FE never changes. The ticket POST still
rides the edge (it IS the auth step).

**Hints, not payloads — the one decision that buys the failure story.** The
stream pushes bare status strings; the FE treats each as "refetch now" and
renders only what the GET returns. Therefore: a publish lost to a Redis
blip costs seconds of staleness (the poll floor still exists); a publish
raced by a rollback costs one harmless refetch; the bus fails OPEN and
lives entirely off the money path. Publishes fire POST-COMMIT from the
three choke points every status write already funnels through
(transition(), begin_cancel_from(), create_order) via a module-level
publisher seam — the otel arming idiom, so no signature changed.

**FR-36's jittered lifetime** (15–30 min, injected rng): the server ends
each stream with an `event: reconnect`, the FE reopens with a fresh ticket
— a fleet's reconnects spread instead of thundering. Heartbeat comments
every 15s keep proxies from reaping quiet streams.

**FE:** while the stream is open the 3s poll idles (`refetchInterval:
false`); ANY stream failure silently returns to polling. Tickets being
single-use means EventSource's built-in reconnect would 401 — so onerror
closes and re-tickets instead.

**Verified live, twice:** (1) curl on /sse/track watched VALIDATED →
PAYMENT_CLEARED → CONFIRMED arrive from the WORKER process (cross-process
via Redis) then ACCEPTED from the kitchen; (2) in the browser, the network
log shows GET order → POST ticket 201 → SSE 200 open, then the kitchen
accepted from OUTSIDE the browser and the tracker advanced to "Cooking"
with exactly ONE hint-triggered GET and zero polling. 693 tests, 100% cov.

**Scale notes:** pub/sub is fire-and-forget by design (a tracking hint has
no value five seconds later — nothing to reap, nothing to store); per-order
channels shard by key hash across a Redis cluster; the subscriber side is
what moves to the dedicated SSE fleet at 400–500k connections (NFR-7), and
the jittered lifetime is what makes that fleet's deploys rollable.
