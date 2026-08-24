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

## S5 — Playwright smoke (the two-window story)

**Two specs against the LIVE stack (deliberately unmocked):** the value is
that a real saga, kitchen, and courier sit behind every click.

1. *The two-window story* (~57s): customer signs in, fills a cart, pays
   with the approving card, watches CONFIRMED arrive; a SECOND browser
   context signs in as the owner, accepts / starts preparing / marks food
   ready in the kitchen feed; the simulated courier's timers run; the
   customer's screen reaches DELIVERED/SETTLED with no further clicks
   anywhere. The entire FR-19 lifecycle, driven through the actual UI.
2. *The declined card*: tok_decline placement still 202s, the saga turns
   the 402 into order state, and the UI shows CANCELLED with the honest
   copy "your card was declined" — never an error page.

**Four bugs the suite's own development bought (each now a comment in the
spec, so the next author doesn't re-pay):**
- Playwright's default ACTION timeout is unlimited — one un-actionable
  click silently consumed the whole test budget and made every failure
  look like a hang. Every click in the flow is now time-boxed.
- The retry loop clicked "Add" while its own previous attempt's modal was
  open — hitting the backdrop and CLOSING it: the loop kept killing its
  own progress. Order now: never click Add when a modal is up.
- **The real root cause:** the modal's "Add to cart" is `disabled` until
  required modifier groups are satisfied (min_select) — and a disabled
  button passes every visibility check while failing every click,
  silently. The loop now picks the chip before the button (seed-agnostic
  XPath) instead of hardcoding option names.
- `getByText("CANCELLED")` strict-mode-collided with the banner containing
  the same word — only when the banner had rendered, so it passed solo and
  failed in the suite. Exact-match on the tag.

Also: `browser.newContext()` inherits nothing from the config's `use`
block — the partner window restates baseURL explicitly.

**npm registry note:** the corporate CodeArtifact registry (expired token)
blocks npx at the repo root; `frontend/.npmrc` already pins the public
registry, so all Playwright installs ran from frontend/. The earlier newman
failure had the same cause.

## S6 — Debezium CDC lane (infrastructure complete; live demo gated on an image pull)

**What outbox_mode=debezium is FOR:** the dev poller is single-instance by
design (ordering) — it is the one component more order-API pods do not
scale. CDC moves publishing into WAL decoding: no polling load on PG,
lower latency, ordered per key, horizontal. Same contract, different
engine.

**Landed and verified:**
- postgres now runs `wal_level=logical` (recreated live; verified with
  SHOW wal_level) — the prerequisite Debezium's pgoutput plugin decodes.
- A `cdc` compose profile with Kafka Connect (no cross-profile depends_on —
  the kafka-exporter lesson; restart-until-broker-up covers ordering).
- The order-outbox connector config, including THE promise from flows.md
  diagram 5: `table.fields.additional.placement=traceparent:header:…` —
  the traceparent column lifted into a Kafka HEADER so the async trace hop
  stays stitched. Plus event_type and aggregate_version headers.
- `make up-cdc` / `make cdc-register` (idempotent PUT), local-dev docs.

**The deliberate scope cut (staff call, documented in local-dev.md):**
events route to a PARALLEL namespace `cdc.c1.orders.events`, not the live
topics. The live topics carry the poller's Avro DomainEvent envelope under
Schema Registry subjects; EventRouter emits a different value shape, and
pointing it at the same subjects would poison SR compatibility for every
consumer. TRUE cutover needs either a custom SMT rebuilding the exact
envelope, or migrating every consumer's serde to Connect-managed schemas —
a coordinated change to make deliberately, not smuggle in at 4am.

**Verified live (morning retry — the overnight stall was pure network, and
quay is the only registry with the tag; Docker Hub's 2.7 does not exist,
which the dead network had masked as a hang):** connector RUNNING, then one
`make demo` while a console consumer watched `cdc.c1.orders.events`:

    id:9ab1bfb3…, traceparent:00-d44d4c66…-660a6b44…-03,
    event_type:OrderPlaced, aggregate_version:0   key=ord_3748a8…
    id:f976e3c6…, traceparent:00-d44d4c66…-c68524ae…-03,
    event_type:OrderConfirmed, aggregate_version:3 key=ord_3748a8…

OrderPlaced and OrderConfirmed carry the SAME trace id in their headers
(different span ids) — the saga's events joined to one trace, lifted from
the outbox column by the EventRouter placement, exactly the diagram-5
promise. Key = order_id (per-order ordering), value = full-state payload.

## The 3am war story (keep this for the mentor)

Recreating postgres for wal_level briefly broke the worker's connection
pool (dead connections from the old server). What happened next, with no
human action: Temporal retried the failing activities, each retry burned
a dead connection out of the pool, the canary went back to green 44s
after the last failure — and BOTH new-tonight alerts (WorkerTargetAbsent,
CanarySilent) went pending during the window and stood down after it.
The system detected, reported, healed, and un-reported an infra bounce
end to end. That is the whole observability + saga story in one incident.

---

## The night in numbers

| Slice | Commit | Suite after it |
|---|---|---|
| S1 hours enforcement | f59e1be | 638 tests, 100% |
| S2 edge rate limiting | 1093798 | 648 tests, 100% |
| S3 analytics service | 00c0618 | 676 tests, 100% |
| S4 SSE live tracking | 3b53733 | 693 tests, 100% |
| S5 Playwright smoke | 69172fb | +2 e2e (~1 min, real stack) |
| S6 CDC lane | d1d78dd | verified live next morning: traceparent headers on cdc.c1.orders.events |

5,736 statements at 100.00% branch coverage; ruff + strict pyright clean
throughout; 12 alert rules ↔ 12 runbook sections in exact parity.

**Bugs found by verifying live, not by the unit gates** (the running theme):
the Avro payload-is-a-string TypeError that parked the whole topic (healed
by our own dlq-replay); the dual OrderPlaced payload shape; the missing
ANALYTICS_BASE_URL in the app-env anchor; the four Playwright traps; and
two alerts built this same day (WorkerTargetAbsent, TracingDisarmed) each
catching a real event within hours of existing.

**Still open, deliberately:** the CDC live demo (image pull — first command
above); the envelope cutover decision (two paths in local-dev.md); rider
utilization (needs dispatch); FE unit tests beyond e2e (Playwright is the
W3 scope); the load test (explicitly parked by the user).

## S7 — Analytics dashboards (owner Insights tab + admin business board)

**Backend adds:** a `totals` lifetime block on the restaurant read —
deliberately a SEPARATE unwindowed query, so the window picker never lies —
with revenue (settled only: a hold is not income), AOV as integer-cents
floor division, distinct customers, and repeat rate (>=2 orders). The ops
read gains windowed revenue. `None`-not-zero discipline throughout: "no
sales yet" and "an average of zero" are different answers.

**Owner UI:** an Insights tab on the Partner dashboard — lifetime cards,
windowed stats with a 7/14/30d picker, and two dependency-free CSS bar
charts (revenue/day; kept-vs-cancelled stacked). The FE does layout math
only — every number arrives computed. One CSS lesson paid: a percentage
height inside an auto-height flex column collapses to nothing; the columns
needed h-full for the bars' % to resolve.

**Admin board:** Grafana speaks Postgres natively, so a `grafana_ro` role
(SELECT-only — proved live: read 868 rows, INSERT denied) plus a provisioned
datasource gives a "Business" dashboard beside the SLO board: orders/day,
revenue/day, cancellation trend, top restaurants, lifetime stats. No new
service; the dashboard is a guest in the database — may look, never touch.

**Verified live:** API self-check (54364¢ / 17 settled = 3197¢ AOV, floor);
the Insights tab rendering the canary's honest red wall of cancels with
green kept-slivers; the business board's top-restaurants table catching
Karim's Kebab House (2 orders) from the Postman onboarding drill.
696 tests, 100% cov.

## S8 — The browse→order funnel (MenuViewed, no new broker)

**The architecture answer made real:** the funnel needed a new event
SOURCE, not a new event processor. `MenuViewed` goes from catalog straight
to Kafka — deliberately NOT through the outbox: a view has no companion
write to be atomic with, browse volume dwarfs order volume, and telemetry
earns different rules (fire-and-forget via the lib's new `send_nowait`,
no-raise, sampled — conversion is a rate and rates survive sampling).
event_id = uuid5(request_id): deterministic per REQUEST, so redelivery
collapses on the consumer's PK while real repeat views stay distinct.

**Analytics** grew a second batch loop (separate group — browse backlog
must never queue ahead of order facts) folding into `menu_views`, and the
conversion computes at read time: distinct signed-in viewers with an order
at that restaurant within 24h of a view, via an EXISTS join against
order_facts. Anonymous views count toward volume only — you cannot join an
order to a browser you cannot name.

**The gap the live test caught:** the first authed view landed with
user_id NULL. Root cause: on public_read routes the edge never verified a
token even when one was PRESENTED — stamping only happened inside the
needs_auth branch. Fix: opportunistic identity — a token offered on a
public route is verified and stamped; a bad/expired one degrades to
anonymous instead of 401 (a stale session must never break public
browsing). Side benefit: the rate limiter now scopes authed public reads
by sub instead of NAT.

**Verified live end to end:** authed view → edge stamp → catalog
fire-and-forget → Kafka → batch fold → order placed by the same user →
funnel read: views 9, viewers 1, converted 1, conversion_rate 1.0 — and
the four funnel cards rendering on the Insights tab. 710 tests, 100% cov.
