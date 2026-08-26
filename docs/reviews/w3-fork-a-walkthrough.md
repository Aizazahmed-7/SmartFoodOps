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

## S9 — The live bell (and the birth of smartfood-realtime)

**The extraction:** order tracking's ticket/pub-sub/stream machinery got
its second consumer, with dispatch (the third) already on the roadmap — so
it graduated into `libs/smartfood-realtime`: the Redis bus + single-use
tickets, and the stream generator (snapshot → hints → heartbeats →
jittered-lifetime reconnect) parameterized by event name and an
`ends_stream` predicate (tracking closes on terminal statuses; the bell
never closes on an event — only the lifetime ends it).

**The generalization made during the lift:** a ticket authorizes a
CHANNEL, not an order. Claims store the fully-qualified channel name, and
each stream endpoint checks the claim against the channel it serves — so a
tracking ticket redeemed at the bell is burned AND refused, structurally.
Tested both directions.

**The bell itself:** channels mirror exactly how the read side scopes
(`_recipient`): customers get sfo:notify:customer:{sub}, owners their
restaurant channel — ONE stream per signed-in identity. The inbox handler
publishes one hint per DISTINCT recipient POST-COMMIT (the transition-hook
rule, third application); the FE bell idles its 15s poll while streaming,
falls back silently on any failure, and a `restaurant` hint also
invalidates the kitchen feed — the owner's queues went near-live for free.
The stream endpoint has NO identity in the URL at all: the claim carries
the channel, so another user's bell cannot even be asked for.

**Verified live:** a curl stream received `event: notify / data: customer`
the moment OrderConfirmed minted the row (worker → Kafka → inbox commit →
hint → Redis → SSE); ticket reuse → 401; in the browser, the owner's tab
sold a ticket after a token refresh, held /sse/notify open, retired the
poll, and refetched notifications + all four kitchen queues on each
confirm — including every canary order ringing the owner's bell, which is
the feature demonstrating itself once a minute. 723 tests, 100% cov.

**Ops footnote from the session:** Docker Desktop reaped most of the fleet
(SIGKILL) between sittings, and uvicorn's --reload masked the damage —
the reloader process keeps a container "Up" while the app inside is dead
awaiting a file change. Force-recreate fixed it; the durable lesson is
that dev containers deserve healthchecks on /healthz so "Up" means
serving, not merely running. Filed for the deploy milestone.

---

## S10 — Receipts + senders (FR-41): the task-queue slice

> Design locked before code (this block is the overnight contract; the full
> narrative + pros/cons follows it once the slice is built and verified).

**The one-sentence design:** when an order SETTLES, the Kafka consumer files a
claim-check row and nudges a Celery chain over RabbitMQ — `render_receipt`
(CPU: PDF → S3 by reference) then `send_receipt` (I/O: mock-mailer, guarded by
a `delivery_log` so at-least-once retries can never re-email) — with a beat
sweeper re-enqueuing anything the nudge lost.

**Decisions (each gets a pros/cons entry below):**
1. Trigger = `OrderSettled` — the first moment the receipt is a *fact* (money
   captured, stock consumed). The bell's silence on OrderSettled stands: this
   mints a document, not an inbox row.
2. Claim check: the consumer persists the full-state payload's receipt fields
   into `receipts` (order_id PK, ON CONFLICT DO NOTHING) — the broker carries
   only `order_id`; tasks never call another service.
3. Delivery idempotency: `delivery_log (order_id, channel) PK` — existence =
   sent; check → send → record (at-least-once; a rare duplicate email beats a
   silently missing receipt). Poison (mailer 4xx) parks via `receipts.failed_at`
   — visible, replayable, never auto-retried.
4. Reliability: post-commit best-effort enqueue (no-raise, like bell hints) +
   a beat sweeper (LEFT JOIN delivery_log, grace window) as the reconciler —
   the DB row is the intent record, so no broker transaction is needed.
5. Two queues (`receipts.render` CPU / `receipts.send` I/O) = the scaling seam;
   two worker containers in compose so the handoff is visible in logs.
6. Celery is sync — tasks get a SYNC engine (psycopg) + sync httpx + boto3,
   built lazily per prefork child (fork safety), injectable for tests.
7. mock-mailer mirrors mock-psp: env knobs + deterministic levers
   (`/admin/fail_next`, magic `@bounce.invalid` recipient) + `/mailer/outbox`.

### How it landed

**The write path** (all of it new tonight): `transitions.py` already stages
`OrderSettled` with full state → the notification consumer, on that one
event type, files `receipts` (the claim check) inside the SAME transaction
as its inbox writes, then — post-commit, best-effort — enqueues
`chain(receipts.render, receipts.send)` with nothing but the order_id.
The renderer worker reads the row, lays the document out as plain text
(`receipt_lines` — testable strings), wraps it in a Courier PDF (fpdf2),
PUTs it to S3 at `receipts/{order_id}.pdf`, records the key, and RETURNS
it — Celery hands that return value to `send`, which checks
`delivery_log`, mails by reference through the `Sender` port, and records
the send. A beat task sweeps `receipts ⟕ delivery_log` every 5 minutes
for anything owed past a 2-minute grace — the reconciler that lets the
enqueue fail freely.

**New moving parts:** RabbitMQ un-parked into core; LocalStack learned
`s3`; `mock-mailer` (port 9081) with FAIL_RATE, `/admin/fail_next` (watch
retries live) and a magic `@bounce.invalid` recipient (watch poison park);
two worker containers — `receipt-renderer` (-Q receipts.render, -B beat)
and `receipt-sender` (-Q receipts.send) — with metrics ports 9109/9110
scraped by Prometheus. ADR-0025 records the decision rule: **side effects
ride a task queue; projections ride the log.**

**Numbers:** 764 tests (41 new), 100.00% coverage, ruff + strict pyright
clean. Ten mermaid diagrams now validate on mermaid@11 — which also
surfaced that semicolons inside message text became parse errors in
current mermaid; fixed across four older diagrams while adding #10.

### The pros/cons ledger — every fork in the road, argued

**1. Broker: RabbitMQ+Celery vs Redis-as-broker vs Kafka vs SQS**

| Option | For | Against | Verdict |
| --- | --- | --- | --- |
| RabbitMQ (chosen) | Real per-message acks (a consumer crash redelivers THAT message); per-queue routing; mature Celery transport; management UI shows queue depth at a glance | One more stateful service to run and learn | The job semantics are native, not emulated |
| Redis as Celery broker | Zero new infra (Redis already runs) | Visibility-timeout redelivery (a slow task can be double-run by design), weaker delivery guarantees under restart, and our Redis already owns two jobs (cache, realtime) — a queue outage would now also be a cache outage | Fine for dev toys; wrong blast radius here |
| Kafka as task queue | Already deployed; infinite retention | Per-partition offsets mean NO per-message ack: one poison "send" blocks its partition or forces manual offset surgery; no per-message retry/backoff; replay — the feature we prize for projections — is precisely the bug for emails | The mismatch that motivated ADR-0025 |
| SQS + Lambda | No broker to operate; the ADR-0008 end-state | Not runnable locally without more LocalStack surface; couples the exercise to AWS | The `Sender`/queue seams keep this a config swap later |

**2. Two chained tasks vs one fat task vs two independent tasks**

- *One fat task* (render+send together): simplest, but a mailer outage
  re-renders the PDF on every retry (CPU burned for an I/O failure), and
  the two halves can't scale or route separately.
- *Two independent enqueues*: no coupling, but send must poll "is the PDF
  ready?" — you've rebuilt the chain, badly, with a race.
- *Chain* (chosen): render's return value IS send's input; a send retry
  re-runs only send; each queue scales on its own axis. Cost: chain args
  ride the broker, so they must stay references — which the claim-check
  rule already demands.

**3. Idempotency ordering: record-after-send (chosen) vs claim-before-send vs status machine**

- *Record after* (chosen): crash between provider-accept and record ⇒ ONE
  possible duplicate email. Receipt always eventually arrives.
- *Claim before*: crash between claim and send ⇒ the log says sent, the
  sweeper skips it, the customer NEVER gets the receipt — silent loss on a
  money document. At-most-once is the wrong direction here.
- *Status machine* (PENDING→SENT + stale-PENDING re-drive): closes the
  window almost fully at the cost of a third state, a second sweeper
  query, and "almost" — the provider-accept-then-crash duplicate still
  exists. Not worth the machinery at this volume; noted as the upgrade
  path if duplicates ever matter.

**4. Payload: claim-check row (chosen) vs data-in-message vs task-calls-services**

Bytes-in-broker bloats RabbitMQ and makes every retry re-serialize the
world; task-calls-order-service adds a runtime dependency + auth surface
to the worker fleet. The claim check costs one row write we were already
positioned to make transactional — and it is why a task needs nothing but
an id to do its whole job.

**5. Trigger: OrderSettled (chosen) vs OrderDelivered vs PaymentCaptured**

A receipt claims "paid in full" — DELIVERED precedes capture, so a receipt
then could precede its own payment (and a capture failure would make it a
lie). PaymentCaptured is keyed by order with no items for the document.
OrderSettled is the first event where the money AND the goods are both
final, and it carries full state.

**6. Enqueue reliability: best-effort + sweeper (chosen) vs outbox-to-broker**

A second outbox (rows → AMQP publisher loop) would give exactly-once-ish
enqueue at the cost of a second publisher daemon. But the receipts row
already IS the durable intent — the sweeper turns it into the retry loop
for free, and the delivery_log makes double-enqueue harmless. Same
reasoning that keeps bell hints transaction-free.

**7. Worker pools: threads (dev, chosen) vs prefork vs gevent**

The self-review caught the trap that decided this: prometheus counters
incremented in PREFORK CHILDREN never reach the parent's /metrics server
(per-process registries) — the scrape wiring would have exported zeros
forever while emails flowed. Dev runs `--pool threads` so tasks execute
in the serving process and the numbers are TRUE; `_get_runtime` gained a
lock because two threads can race the first lazy build. The ledger:
prefork is the prod answer for CPU-bound render (real parallelism, needs
`PROMETHEUS_MULTIPROC_DIR` for metrics); threads are honest for dev and
fine for I/O; gevent is the send-side upgrade when volume demands
hundreds of concurrent HTTP sends, at the cost of monkey-patching next to
psycopg and boto3. The queue split makes any of these a one-container
change.

**8½. Recipient email: resolve-at-send vs copy-at-consume vs identity projection** *(added in the morning session — the synthesized `{user_id}@customers.smartfood.dev` stub was replaced with the real thing)*

- *Resolve at SEND time* (chosen): the send task asks Identity's new
  `GET /v1/internal/users/{id}` (system-authed, narrow ContactOut
  contract) through a `Contacts` port with the mailer's two-exception
  error shape — `ContactsUnavailable` (5xx/network) joins `autoretry_for`,
  `UnknownRecipient` (404) parks via `failed_at` (`no_recipient` outcome,
  ReceiptParked alert widened). Pros: always the CURRENT address (email
  changed after ordering → receipt goes where they read mail now); no PII
  copies; the task queue absorbs the new dependency natively (retries
  with backoff are its whole job). Cons: identity outage delays sends —
  by design, into the retry schedule. The dup-check runs FIRST so an
  already-sent receipt never costs an identity round trip.
- *Copy the email into the claim check at consume time*: no send-time
  dependency, but the lookup moves into the Kafka consumer path — where a
  dependency stall poisons batch progress toward the DLQ — and it freezes
  a stale address into a PII copy in a second database.
- *Project an identity event stream locally* (an order_recipients for
  emails): no runtime dependency at all, but identity publishes no user
  events today, and the stream would either carry PII in immortal events
  (the thing we refuse) or be a change-ping that still ends in a lookup.
  The projection is the right shape at much higher send volume; noted as
  the scale path.

**9. Scheduling the sweep: beat (chosen) vs cron vs Temporal schedule**

Beat rides the renderer worker (`-B`, exactly one in the fleet — two
beats would double every sweep). Cron would need a container with repo
context anyway; a Temporal schedule would put a reconciliation loop on
the orchestrator that owns customer-facing sagas — wrong neighborhood for
a janitor. If workers ever scale horizontally, beat moves to its own
container: the known constraint, documented in compose.


### Verified live — four drills against the running stack

1. **Happy path** — `ord_39215ec0…` driven place→SETTLED: consumer minted
   the claim check and nudged — renderer logged `receipt rendered` (0.72s)
   and RETURNED the key — sender received that key as its first argument
   IN A DIFFERENT CONTAINER (the chain handoff, live), mailed, recorded
   `msg_202ae659…`. The PDF pulled back out of S3 is a real 1-page,
   1254-byte document: aligned money column, totals block, em-dash intact
   (the cp1252 fix). The mailer outbox held exactly that one email, with
   `attachment_key` as a reference.
2. **Duplicate protection** — re-enqueued `receipts.send` for the same
   order via the celery CLI: `receipt already sent — skipping`, the
   ORIGINAL message id returned, outbox count unchanged. delivery_log
   doing its one job.
3. **Provider outage** — `/admin/fail_next 3` then a fresh order: three
   `MailerUnavailable` retries, then success on attempt four. First run
   exposed celery's full-jitter rolling `Retry in 0s` three times —
   switched to deterministic backoff and re-drilled: `Retry in 2s`,
   `Retry in 4s` at exact spacing, then `receipt sent`. One email.
4. **Lost enqueue** — planted a receipts row 10 minutes old with no
   delivery_log entry (exactly the residue of a dead-broker moment). The
   01:36 sweep returned 0 (quiet baseline); the 01:41 sweep found it,
   WARNed `re-enqueued unsent receipts count=1`, and 127ms later the
   receipt was rendered, stored, and mailed. No human.

5. **Real recipient (morning session)** — after replacing the
   synthesized address with send-time Identity resolution: a fresh
   settle produced `to: customer@demo.smartfood.dev` in the outbox, and
   identity's log shows the worker's system-authed
   `GET /v1/internal/users/usr_9837…` → 200 in 7.8ms.

Worker /metrics confirmed truthful under the threads pool: renderer
`receipts_rendered_total 2`, sender `receipts_sent_total{sent} 2` +
`receipts_swept_total 1` — the exact drill history. `promtool` validates
14 alert rules (ReceiptsSweeperBusy + ReceiptParked joined, each with a
runbook section). `receipt_enqueue_failures_total` on the service: 0.

**Suggested commit** (you commit, as always):

```
feat(notification): receipts pipeline — Celery/RabbitMQ senders (S10, FR-41, ADR-0025)

OrderSettled now owes the customer a PDF receipt, delivered by the
first task-queue flow in the fleet: the Kafka consumer files a
claim-check row in the event's own transaction and nudges a Celery
chain over RabbitMQ — render (PDF → S3 by deterministic key) hands
its s3 key to send (delivery_log-guarded mail through the Sender
port). Enqueue is post-commit best-effort; a beat sweeper re-enqueues
anything owed past a grace window, so a lost nudge costs minutes,
never the receipt. Mailer 5xx retries with deterministic backoff;
4xx parks via failed_at (clearing it is the replay lever).

Recipients resolve at SEND time against Identity's new internal
contact read (system-authed, narrow contract) — events stay PII-free
and the customer gets the receipt at the address they use NOW; 404
parks as no_recipient, 5xx rides the same retry schedule.

New: mock-mailer tool (fail_next/fail_rate/bounce levers), receipts +
delivery_log tables (migration 0002), receipt-renderer/receipt-sender
workers (threads pool — prefork children hide their counters from
/metrics), rabbitmq un-parked to core, LocalStack +s3, two alert
rules + runbooks, flows.md diagram 10 with [AMQP]/[S3] legend rows,
ADR-0025. 770 tests, 100% coverage.
```
