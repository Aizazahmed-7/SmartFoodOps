# SmartFoodOps — The Full Architecture, Explained

**What this document is**: a narrative walkthrough of the entire Part A architecture — every database, every background workflow, and why each piece exists. [ARCHITECTURE.md](ARCHITECTURE.md) is the terse reference; this is the guided tour. If they ever disagree, the reference wins.

---

## 1. The system in one paragraph

SmartFoodOps is designed around a single observation: **an order is a distributed transaction** spanning stock, money, a restaurant's decision, and a rider's journey — so the architecture is organized around the order lifecycle, not around CRUD services. One Temporal workflow owns each order's saga from placement to settlement. Kafka carries immutable facts about what already happened, fed exclusively through transactional outboxes. Celery does loss-tolerant chores. Everything else — eleven microservices, five databases in one Aurora cluster, two dedicated clusters, six DynamoDB tables, one namespaced Redis, and a CDN — exists to serve that lifecycle at a 2,500 orders/s ceiling without ever corrupting an order or a payment.

## 2. The three invariants

Every design choice below traces back to three rules, enforced in code review and tested in CI:

1. **Every state change commits via transactional outbox before it is announced.** The event row is written in the same database transaction as the state change, so "it happened" and "everyone was told" cannot diverge.
2. **Every side-effectful call carries an idempotency key.** Client keys at the edge, money keys `{order_id}:{op}` at Payment, conditional writes at Dispatch.
3. **Every consumer is at-least-once + idempotent.** "Exactly-once" is engineered end-to-end (guarded transitions, dedupe tables, conditional writes) — never assumed from a broker.

And one division of labor, which decides where any new piece of work goes:

| Component | Role | Litmus test |
|---|---|---|
| Temporal | Decides what happens next | If losing or double-running it could corrupt an order or money → Temporal activity or outbox event. Never a bare REST call or Celery task. |
| Kafka | Announces what already happened | Consumers react to facts; they never drive the saga. |
| Celery + RabbitMQ | Does the chores | If losing the task is merely annoying → Celery is fine. |

## 3. Anatomy: who talks to whom

Four portals: the customer app, restaurant portal, and admin console enter through CloudFront (browse traffic is largely absorbed there); the realtime traffic — rider WebSockets and customer tracking SSE — enters through a dedicated ALB on its own subdomain (`rt.api`), never through CloudFront. Not because CloudFront couldn't proxy those connections — it can — but because a long-lived stream has nothing to cache, CloudFront's origin-timeout model adds friction against the 20-second heartbeats, and a separate ALB keeps a realtime incident and a REST incident from sharing a blast radius. Listener rules fan the realtime ALB out: `/ws/rider/*` to rider-gateway, `/sse/track/*` to tracking-gateway; everything else reaches edge-bff via CloudFront → the API ALB.

| Tier | Compute | Why this compute |
|---|---|---|
| edge-bff | ECS Fargate, 2–15 autoscaled tasks | Stateless request work; scheduled pre-scale before meal peaks |
| Domain services (Identity, Catalog, Inventory, Order, Payment, Dispatch, Notification, Analytics) | ECS Fargate | Sustained request/stream load — Lambda's per-invoke pricing and connection pooling lose here |
| rider-gateway, tracking-gateway | ECS on EC2 | Tens of thousands of long-lived connections per node need kernel/ENI tuning |
| Temporal workers, Kafka consumers, Celery workers | ECS Fargate | Long-poll and stream shaped, never idle during service hours |
| Notification senders, DDB Streams forwarder, cache bump, webhooks, reports, Firehose transforms | Lambda | Bursty, event-triggered, loss-tolerant — the canonical serverless fit |

The edge verifies JWTs exactly once (RS256, against Identity's JWKS), strips and re-stamps `X-Auth-*` identity headers, and applies admission control — a Redis token bucket that returns 429 *before any state is written* when the cell is over its load-tested budget. Domain services have no public routes; the network is what makes the identity headers trustworthy. Ownership checks live in the owning service, in the query itself (`WHERE restaurant_id = :ctx.restaurant_id`; 0 rows → 404).

Pricing is deliberately **not** a service ([ADR-0015](adr/0015-pricing-is-a-library-not-a-service.md)): it owns no data, has one caller, and scales on its caller's curve. It ships as `libs/smartfood-pricing`, embedded in the Order workers (authoritative snapshot) and Order's stateless `/v1/quote` endpoint (the display estimate the client-side cart requests — ADR-0017) — one implementation, so the price shown and the price charged cannot drift.

## 4. The order saga

`OrderWorkflow` (ID `ord::{order_id}`, duplicate starts rejected and attached) is the single writer of order state. Temporal records every step into a durable event history; if a worker dies mid-order, another replays the history and continues — timers included. The three-minute restaurant-acceptance window is a server-side durable timer, and the restaurant's accept arrives as a signal, not a blocking call.

```
PLACED → VALIDATED → PAYMENT_CLEARED → CONFIRMED → ACCEPTED
      → PREPARING → READY → PICKED_UP → DELIVERED → SETTLED
any pre-PICKED_UP → CANCELLING → CANCELLED (→ REFUNDED if captured)
```

The activities, in order: **PriceOrder** (in-process local activity; writes the immutable pricing snapshot all money math derives from) → **ValidateAndReserve** (restaurant capacity + atomic conditional stock decrement) → **AuthorizePayment** (idempotency-table read-first, so an unknown-outcome retry can never double-charge; success is `PAYMENT_CLEARED` — the state name is method-agnostic, and the `PaymentAuthorized` event name is unchanged) → **ConfirmOrder** (visible as confirmed only after everything succeeded) → durable wait, with the restaurant alerted by the Notification consumer of `OrderConfirmed` rather than by a workflow activity. Accept spawns the `DeliveryWorkflow` child; delivery completion triggers **CapturePayment** → **Settle**. Money moves only at capture — cancel before pickup and there is only an authorization to void, nothing to refund.

On failure, the saga unwinds in reverse: void authorization → release reservation → CANCELLED, with the cancellation event fanning out to notifications and analytics. Compensations retry indefinitely (5-minute backoff cap), alert at 10 attempts, and page a human at one hour — a compensation is never silently dropped. The mock PSP's failure injection (`DECLINE_RATE`, `TIMEOUT_RATE`, `UNKNOWN_OUTCOME_RATE`, magic card tokens) makes every one of these paths a CI test, not a hope.

---

## 5. The databases — every store, and why it exists

The governing rule: **choose the store per workload, and one owner per piece of data.** A service only ever touches its own store; everyone else goes through its API or its Kafka topic. The full per-service map is [service-ownership.md](service-ownership.md); this section explains the reasoning.

### 5.1 The relational tier: three Aurora clusters

A **cluster** is machines; a **database** is an isolated compartment inside one. Separate databases give schema ownership (Postgres physically cannot join across databases from one connection); only separate clusters give performance isolation. [ADR-0016](adr/0016-postgres-topology-one-cluster-database-per-service.md) chooses deliberately:

**`sfo-aurora-main`** — one cluster, five logical databases, each with its own role and no cross-database grants, all behind PgBouncer transaction pooling (RDS Proxy only for Lambdas — ADR-0016):

| Database | Owner | What lives there | Why Postgres |
|---|---|---|---|
| `identity_db` | Identity | users, roles, addresses, refresh-token families | Refresh-token rotation and reuse detection need transactional integrity |
| `catalog_db` | Catalog | restaurants, menus, `menu_categories` (categories → items → modifiers), `menu_versions`, promo rules, tax tables | Menu edits — categories included — bump the version **in the same transaction** as the rows — the anchor of the cache-invalidation design |
| `inventory_db` | Inventory | stock counters, `restaurant_load` capacity counter, reservation ledger | The atomic conditional decrement (`UPDATE … WHERE available >= q`) is the oversell guard; restaurant capacity uses the identical pattern (`WHERE active < capacity`) — kitchen slots as stock |
| `order_db` | Order | orders + pricing snapshots, hour-partitioned outbox, restaurant order feed index | The state machine's guarded transitions are SQL `UPDATE … WHERE status='prev'`; the outbox must share the order's transaction |
| `payment_db` | Payment | append-only double-entry ledger (7y), idempotency table | Money. Nothing else was ever considered |

A service graduates to its own cluster at ~30% of the cluster write budget or when its access pattern degrades neighbours. Order is the expected first mover — its cell-prefixed ULIDs carry reserved shard bits, so the split is a routing change, not a migration.

**`sfo-aurora-analytics`** — dedicated cluster. Kafka consumers upsert aggregates in 5-second micro-batches while Grafana runs long reads; both would pollute the OLTP buffer pool if co-located.

**`sfo-aurora-temporal`** — dedicated cluster for Temporal's persistence. Correctness-critical, with its own shard-count and write-amplification profile.

### 5.2 DynamoDB: high-volume key-value, named for its owner

Every table has a uniform-cardinality partition key. One hard rule, enforced in design review: **no restaurant-, city-, or status-keyed partition key or GSI anywhere** — the only plausible hot partitions in the system, banned outright.

| Table | Owner | Key / TTL | Why DynamoDB |
|---|---|---|---|
| `sfo-order-tracking` | Projectors | order-keyed, TTL post-terminal | The read model millions of tracking screens hit — key-value reads scaled independently of the write path |
| `sfo-order-history` | Projectors | `PK=<customer>, SK=<ts>#<order>` | Customer history list, one query per screen |
| `sfo-dispatch-deliveries` | Dispatch | delivery-keyed + rider GSI | Delivery records at fleet volume |
| `sfo-dispatch-rider-state` | Dispatch | rider-keyed | **The conditional-write assignment lock** — the single authority that makes double-assigning a rider impossible |
| `sfo-rider-locations` | rider-gateway | day-bucketed, TTL 30d | GPS breadcrumb firehose |
| `sfo-notification-log` | Notification | notification-keyed, TTL 90d | The dedupe record behind exactly-once user-visible notifications |

`sfo-order-tracking` and `sfo-order-history` are read models, not caches: durable, rebuildable by replaying Kafka (a runbook, not a cache warm), and the primary serving path. Dispatch is the one DynamoDB-owned *domain* service, which is why its events reach Kafka differently (§6).

### 5.3 Redis: one cluster, namespaced by owner

`global-redis` (ElastiCache, 3 shards × primary+replica) is shared hardware partitioned by **keyspace prefix**: each service may only write `<its-own-name>:*`, enforced at runtime by the shared `cache_client` and server-side by Redis ACL key patterns. Everything in it is ephemeral — **loss degrades, never corrupts** — and every key carries a TTL (a CI lint rejects `SET` without expiry).

| Prefix | Owner | Contents |
|---|---|---|
| `catalog:*` | Catalog | Versioned menu blobs + current-version pointers (invalidation = pointer swap, zero stampede), browse pages, singleflight locks |
| `edge:*` | edge-bff | Rate-limit buckets, per-cell placement admission tokens |
| `order:*`, `payment:*` | Order, Payment | Idempotency fast-paths (15 min; Postgres keeps 7 days and stays the arbiter) |
| `inventory:*` | Inventory | Per-item admission buckets shielding the stock row from viral-item stampedes (advisory — Postgres decides) |
| `shared:*` | one named writer each | The five sanctioned cross-service keys: `shared:geo:*` (rider GEO index, written by rider-gateway, read by Dispatch), `shared:loc:*`, `shared:hb:*` (liveness — expiry *is* the signal), `shared:trk:*` (tracking pub/sub), `shared:ticket:*` (SSE ticket handoff) |

Notation note: `{rid}` in a key is a literal Redis hash tag (forces colocation on one shard — used to keep a menu's blob and pointer together); `<rid>` means substitute a value. `shared:geo:<cell>:<gh4>` deliberately has no hash tag so geo shards spread across nodes.

Money, order state, and stock are **never served from Redis**. The pricing and inventory activities re-read their sources of truth at placement, which is why a stale cache can annoy but never mischarge.

### 5.4 The CDN and the lake

**CloudFront** is a cache, not storage: menus are immutable per version (`/v1/menus/<rid>/v/<ver>`, 7-day TTL, never purged — a new version is a new URL), browse pages live 30 seconds and carry the current menu version, which is how clients discover new versions. Authenticated responses are `private, no-store` with caching disabled — a shared CDN caching one user's order status would serve it to the next user.

**S3 lake** — every Kafka event lands as Parquet via an MSK Connect S3 sink (raw), with Firehose + Lambda producing curated transforms. This is the analytics replay source, the GDPR crypto-shredding boundary, and Part B's training/feature corpus.

---

## 6. The event backbone

The dual-write problem, in one sentence: "commit to Postgres, then publish to Kafka" has a crash window between the two operations that either loses the announcement or announces a rollback — and no code ordering fixes it. The outbox dissolves it: the event is a row in the same transaction as the state change, and a relay moves committed rows to Kafka afterwards.

- **Postgres-owned services** (Identity, Catalog, Inventory, Order, Payment): Debezium on MSK Connect tails the write-ahead log and publishes outbox rows. Locally, an in-process poller emits byte-identical records (`OUTBOX_MODE=poller|debezium`; CI runs Debezium to enforce parity).
- **Dispatch** (DynamoDB-owned): DynamoDB Streams *is* its outbox — the stream record is produced atomically by the same write that changed the item. A forwarder Lambda ships records to Kafka in the shared envelope (`DISPATCH_FORWARDER=poller|lambda` locally, because LocalStack's Streams→Lambda trigger is unreliable).
- **Sanctioned exception**: `rider.locations` and `rider.status` are produced directly by rider-gateway. GPS telemetry has no database state to be atomic with — we already discard four of five pings by design — so there is no inconsistency for an outbox to prevent. Every domain-fact topic keeps the rule.

Topics in `global-kafka` (MSK + Confluent Schema Registry, Avro, `BACKWARD_TRANSITIVE` compatibility gated in CI):

| Topic | Key | Partitions | Carried events (the brief's eight, mapped) |
|---|---|---|---|
| `c1.orders.events` | order_id | 48 | OrderPlaced, OrderConfirmed, OrderPickedUp, OrderDelivered, OrderCancelled |
| `c1.payments.events` | order_id | 12 | PaymentAuthorized, PaymentCaptured (the brief's "PaymentSuccessful"), RefundProcessed |
| `c1.dispatch.events` | delivery_id | 48 | RiderAssigned, offer/arrival markers |
| `c1.rider.locations` / `c1.rider.status` | rider_id | 12 each | sampled GPS (0.2 Hz), shift status |
| `c1.catalog.changes` | restaurant-scoped, compacted | 6 | menu CDC — feeds the cache renderer and Part B embeddings |
| `c1.inventory.events` / `c1.identity.events` | aggregate id | 12 / 6 | stock movements; account audit |

Every event carries `event_id` (a deterministic UUIDv5 dedupe key derived from `aggregate:{id}:{version}:{type}` — a retried emit reproduces the same id instead of minting a new one), `aggregate_version` (projectors apply only if newer — a late `OrderConfirmed` after `OrderCancelled` no-ops), and W3C `traceparent` in headers so traces survive the async hop.

Failure handling splits by consumer class: ordering-tolerant consumers (analytics, notifications, sinks) walk retry tiers (`retry.1m` → `retry.10m`) into a per-group DLQ; ordering-sensitive projectors instead **pause the partition** and page — a retry topic would reorder a key's events. Poison pills go straight to the DLQ with the error class, offsets, and trace context attached.

---

## 7. Background processing — everything that runs off the request path

This is the "reliable background processing" the brief asked for, and it is deliberately **four different machines** for four different guarantees:

| Machinery | Guarantee | Used for |
|---|---|---|
| Temporal workflows | Durable, exactly-once decisions, survives crashes mid-flow | The order and delivery sagas, compensation, refunds |
| Kafka consumers | At-least-once, replayable, independent of user flows | Read models, notification decisions, analytics |
| Celery + RabbitMQ | At-least-once *delivery attempt*, loss-tolerant | Chores whose loss is annoying, not corrupting |
| Lambda | Event-triggered burst | Sends, forwarding, transforms |

### 7.1 Temporal: the workflows

- **`OrderWorkflow`** — one per order (§4). Owns every state transition, the acceptance timer, and the compensation stack.
- **`DeliveryWorkflow`** — child of OrderWorkflow, started on restaurant accept. Runs candidate search (Redis GEO → `rider_state` filter → scoring), the offer protocol (DDB conditional-write lock, 15s/12s/12s cascade, widening radius → surge → ops escalation), arrival geofencing, pickup deadline, and the reassignment ladder (gateway disconnect signal → heartbeat-expiry sweeper → 60s reconciliation scan). Cancellation of the parent cancels the child.
- **`PostDeliveryRefundWorkflow`** — post-pickup cancellations, which are support policy rather than automatic compensation (no inventory rollback once food left the restaurant).

The saga's Temporal footprint is budgeted: **≤12 activities + ≤3 timers + ≤4 signals** across `OrderWorkflow` + `DeliveryWorkflow` on the happy path — `PriceOrder` runs as a local activity, restaurant notification is an `OrderConfirmed` consumer instead of an activity, and guarded transitions fold into their owning activities. The replay suite counts commands against the budget (warn-only from W2, a merge gate before Phase 3), because actions-per-order is the single biggest Temporal cost and throughput driver.

Workers run on Fargate polling task queues, with **separate task queues per activity class and reserved compensation workers** — so a flood of new placements can never starve the unwinding of failed ones. `activity_schedule_to_start_latency` is the starvation early-warning metric and the autoscaling key.

### 7.2 Kafka consumers: the reaction fleet

- **Read-model projectors** — build `sfo-order-tracking` and `sfo-order-history` from `orders.events` + `dispatch.events`, version-guarded. Lag SLO p99 < 2s.
- **Notification decider** — consumes all domain topics, decides what each event means for whom, dedupes against `sfo-notification-log`, and enqueues send jobs into RabbitMQ. Deciding is Kafka-consumer work (must not be lost); sending is a chore (retryable, provider-buffered).
- **Analytics aggregator** — consumes everything, aggregates in memory, upserts to `sfo-aurora-analytics` in 5-second micro-batches. All nine required metrics come from this one pipeline: totals and per-restaurant counts, peak-hour load, delivery-time digests, rider utilization, cancellation/acceptance/success rates, and failed-events/workflow-issues (DLQ depth, `illegal_transition_total`, stuck-workflow gauge). A nightly Athena job recomputes from the S3 lake and alarms on >0.1% drift — the streaming numbers are continuously audited against the batch truth.
- **Part B hooks** — `partb.embeddings` (on `catalog.changes`, 30s per-restaurant debounce) and `partb.features` (on `orders.events`) are reserved consumer groups: the GenAI phase lands as ordinary consumers with zero Part A changes.

### 7.3 Celery + RabbitMQ: the chores, in detail

Why does this tier exist at all, when we have Kafka and Temporal? Because both are the wrong tool for disposable work. Temporal's durable history is overhead for a receipt PDF; Kafka is a *log* — it retains, replays, and preserves order, none of which a thumbnail job wants. RabbitMQ (Amazon MQ in AWS, the management-UI container locally) is a **task queue**: a job is delivered to exactly one worker, acknowledged, and gone. Different semantics, deliberately paired with work where those semantics fit.

The admission test is the litmus from §2, inverted: **a job may only go to Celery if losing it is merely annoying.** Every Celery job must also be idempotent, because delivery is at-least-once (a worker that dies mid-job causes redelivery).

The job catalog:

| Queue | Jobs | If lost |
|---|---|---|
| `notifications` | Render + dispatch send jobs to provider Lambdas (push/SMS/email) | Customer misses one notification; the tracking screen still shows the truth. The *decision + log* already happened in the Kafka consumer — only the send attempt is disposable |
| `documents` | Receipt PDFs, invoice generation | Regenerable from the order + ledger on demand |
| `media` | Restaurant image resize/thumbnail variants | Original is in S3; re-derive |
| `reports` | Scheduled restaurant performance reports, admin exports | Re-run |
| `warmup` | Menu-cache pre-warming before meal peaks, browse-page pre-render for hot geohashes | Pure optimization; a cold cache self-heals via singleflight |

Flow for the biggest one: Kafka event → Notification decider (dedupes, logs, decides channel) → RabbitMQ `notifications` queue → Celery worker renders the payload → invokes the sender Lambda → provider. Celery tasks are OTel-instrumented (trace context rides the message headers, so a send job stitches into its order's trace), and queue depth plus per-queue task-failure rate are first-class Prometheus signals. Retries with exponential backoff; a job that exhausts retries lands in a dead-letter queue with an alert — visible, but never blocking anything upstream, and never able to double-send thanks to the `notification_log` dedupe on the decider side.

Celery workers run on Fargate, scale on queue depth, and are the *only* consumers of RabbitMQ. Nothing correctness-bearing is allowed in this tier, and that is enforced by review against the litmus test — the whole point of having a designated place for disposable work is that it keeps disposable work out of the machinery that guarantees things.

### 7.4 Lambda: the burst edges

Notification senders (fan-out to providers), the DDB Streams forwarder (Dispatch's outbox relay), menu-cache version bumps, PSP webhook receivers (the mock PSP's async `UNKNOWN` outcomes arrive here, exercising reconciliation), admin report generation, and Firehose transforms. All bursty, all loss-tolerant or replayable from their trigger source.

### 7.5 Housekeeping loops

The unglamorous processes that keep the system honest: the **reservation expiry reaper** (releases stock held by orders that never completed), the **outbox partition dropper** (drops hour partitions ≤6h after publish confirmation — never row-deletes at 20k rows/s), the **rider liveness sweeper** (leader-elected, driven by heartbeat-expiry keyspace notifications, with a 60s SCAN reconciliation as the guarantee), **synthetic canary orders** (one per minute, full saga, probed from outside), and the **nightly drift check** (lake vs streaming aggregates).

---

## 8. The real-time planes

**Riders: WebSocket, bidirectional.** One socket per rider (JWT in `Sec-WebSocket-Protocol`, connection bound to `rider_id` — GPS frames are attributed from connection state, never payload). Per 1 Hz ping: GEO index update, latest-loc + heartbeat refresh, in-memory geofence check (→ `rider_arrived` signal at 75m), every 2nd ping to the tracking pub/sub channel, every 5th to Kafka. Delivery offers come down the same socket.

**Customers: SSE, one-way.** `EventSource` can't set headers, so connect is ticket-authed (single-use, 60s, `GETDEL`). Every connect and reconnect serves a snapshot from the durable `sfo-order-tracking` read model *first*, then subscribes live — so a dropped pub/sub message or a Redis restart can never strand a screen on stale state. Fallback chain: read model → Temporal workflow query (rate-limit-guarded) → `order_db`. Milestone changes push over the same stream; the customer never reloads.

## 9. Peak-hour survival

Scaling, in order of engagement: CloudFront absorbs browse → scheduled ECS scaling raises task floors ~15 minutes before meal windows → target tracking adds edge-bff/service tasks on requests-per-target and CPU → DynamoDB floors are pre-warmed (on-demand ramp is too slow for a 10-minute spike) → and when demand still exceeds the load-tested budget, the edge admission bucket sheds with 429s *before any state is written*.

The degradation ladder (1–4 automated, 5–6 ops-approved): CDN serves stale browse → pause analytics/Part B consumers (Kafka lag is the cheapest currency) → GPS Kafka sampling 0.2→0.05 Hz → tracking cadence 2s→5s → stale menu cache → restaurant capacity gating. The invariant behind it: **the money path is queued, never shed** — in-flight sagas always run, with Temporal's backlog as the sanctioned buffer.

## 10. Observability

One trace per order, stitched across every hop: outbox rows store `traceparent` as columns and the publisher lifts them into Kafka headers; Temporal's tracing interceptor is replay-safe; `workflow_id = order_id` is a search attribute, so Jaeger and Temporal Web cross-navigate on the same key. Tail-based sampling keeps 100% of error, slow, payment, and dispatch traces. Structured JSON logs carry `trace_id` + `order_id` on every line.

Prometheus scrapes every service; Grafana renders and alerts (Amazon Managed Prometheus + Grafana on AWS — same PromQL locally and in prod). The signals that matter: `illegal_transition_total` (~0 always; any spike is an idempotency bug), `outbox_publish_lag_seconds`, `ledger_imbalance_cents` (≠0 pages), consumer-lag *derivative* (falling behind vs merely behind), `activity_schedule_to_start_latency` (worker starvation), DLQ depth and age, `IteratorAge` on the Streams forwarder, and the per-minute canary order. Every alert ships with a runbook URL, a dashboard, and a pre-built Jaeger query — an alert without a runbook fails CI lint.

SLOs: placement 99.95% availability, p95 < 3s (p99 PLACED→CONFIRMED < 6s); dispatch READY→ASSIGNED 95% < 90s; tracking device-to-screen 99% < 5s; notifications 99% < 30s; projector lag p99 < 2s.

## 11. Where each brief requirement lands

| Brief ask | Where it lives |
|---|---|
| Slow/manual ordering workflows | The saga (§4): automated validation → auth → confirm, p99 < 6s |
| Inconsistent menu/inventory data | Versioned menus + same-tx version bumps + CDC invalidation (§5.1, §5.4); atomic stock decrements |
| Peak-hour latency | §9: CDN, pre-scale, admission control, shed ladder |
| Inefficient dispatch | DeliveryWorkflow + GEO candidate search + conditional-write lock (§7.1) |
| Unreliable notifications | Decide-vs-send split with dedupe log (§7.2, §7.3) |
| Fragmented analytics | One event pipeline → nine metrics + drift audit (§7.2) |
| Underutilized event data | Outbox → Kafka → S3 lake + Part B hooks (§6) |
| Reliable background processing | Four machineries with explicit guarantees (§7) |
| Observability & failure recovery | §10; compensation + reassignment + chaos CI throughout |

Requirement-by-requirement traceability with FR numbers is in [PRD.md](PRD.md) §6.
