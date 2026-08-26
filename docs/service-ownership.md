# Service ownership reference

What each component owns, reads, and publishes. The governing rule: **one owner per piece of data, and a service only reads its own store.** If a service needs someone else's data it calls that service's API or consumes its Kafka topic — never its tables, never its Redis keys.

## Naming convention

Every datastore is named for its owner, so the name answers "whose is this?" without a lookup.

| Kind | Name | Example |
|---|---|---|
| Logical database in the shared cluster | `<service>_db` in `sfo-aurora-main` | `catalog_db` — "catalog-postgres" |
| Dedicated Aurora cluster | `sfo-aurora-<name>` | `sfo-aurora-analytics` |
| DynamoDB table | `sfo-<owner>-<entity>` | `sfo-dispatch-deliveries` |
| Redis keyspace in `global-redis` | `<owner>:…` | `catalog:menu:…` |
| Shared Redis key (three only) | `shared:…` | `shared:geo:…` — the prefix *is* the warning |
| Kafka topic in `global-kafka` | `<cell>.<domain>.events` (cell-prefixed from day 1, one cell today) | `c1.orders.events` |

**Placeholder notation**: `<name>` means substitute a value. Literal braces `{name}` mean a **Redis cluster hash tag** and are load-bearing — they force keys onto one shard. `catalog:menu:{rid}` colocates a restaurant's menu and its render lock on purpose; `shared:geo:<cell>:<gh4>` has no tag on purpose, so geo shards spread across nodes rather than collapsing onto one.

**Short names vs deployed names**: prose throughout the docs refers to a table by its entity name (`rider_state`, `order_tracking`); the deployed AWS resource is `sfo-<owner>-<entity>` (`sfo-dispatch-rider-state`, `sfo-order-tracking`). The per-service tables below are the authoritative mapping. Redis keys are always written in full, prefix included — there the prefix *is* the ownership statement and the thing `cache_client` enforces.

## Shared infrastructure

| Name | What | Notes |
|---|---|---|
| `sfo-aurora-main` | One Aurora PostgreSQL cluster, one logical DB per service, per-service PgBouncer (transaction mode) in front — RDS Proxy for Lambdas only | Schema ownership, **not** performance isolation — [ADR-0016](adr/0016-postgres-topology-one-cluster-database-per-service.md) |
| `sfo-aurora-analytics` | Dedicated cluster | Long analytical reads would pollute the OLTP buffer pool |
| `sfo-aurora-temporal` | Dedicated cluster | Temporal persistence; correctness-critical, distinct write profile |
| `global-redis` | One ElastiCache cluster, partitioned by keyspace prefix | `cache_client` refuses keys outside the service's namespace; Redis ACLs enforce it server-side |
| `global-kafka` | MSK + Schema Registry | Topics owned per domain; **no service produces directly** except the telemetry exception below |
| DynamoDB | Regional service — tables, not clusters | Owner encoded in the table name |

**Redis cluster groups** (ADR-0018, D5 trigger): every keyspace prefix carries a **cluster-group** annotation — the cluster it lands on if the trigger-gated three-way split fires. One cluster today; the split is config, not code, because `cache_client` resolves the endpoint per namespace.

| Group | Prefixes |
|---|---|
| **money** | `edge:rl:*`, `edge:adm:place:*`, `order:idem:*`, `payment:idem:*`, `shared:ticket:*` |
| **realtime** | `shared:geo:*`, `shared:loc:*`, `shared:hb:*`, `shared:trk:*`, `inventory:adm:*` |
| **catalog** | `catalog:menu:*` (rendered menu, cache-aside), `catalog:browse:*`, `catalog:lock:*` |

Compute legend: **MS** = ECS Fargate microservice · **GW** = ECS on EC2 connection gateway · **λ** = Lambda · **W** = worker (no inbound API).

---

## edge-bff — MS, :8000

The only service with no persistent data of its own. That emptiness is what lets it scale to N identical tasks.

| | |
|---|---|
| Postgres | none |
| DynamoDB | none |
| Redis | `edge:rl:<scope>:<subject>` — rate-limit buckets (per-IP, per-user, login)<br>`edge:adm:place:<cell>` — order-placement admission tokens<br>`shared:ticket:<rand>` — **writes** single-use 60s SSE tickets |
| In-process | JWKS public keys (10 min, background refresh)<br>Hot-menu LRU — caches *Catalog's API responses* keyed by `(rid, ver)`, never Catalog's Redis keys |
| Kafka | produces nothing, consumes nothing |
| Calls | Identity (JWKS), Catalog, Order |

## Identity — MS, :8001

| | |
|---|---|
| Postgres | **`identity_db`** — `users`, `roles`, `addresses` (**soft-delete only** — orders snapshot the delivery address and must survive an address deletion, ADR-0018), `refresh_tokens` (family-tracked for reuse detection), `outbox` |
| Redis | none |
| Kafka | **produces** `identity.events` (outbox → Debezium) |
| Serves | `/.well-known/jwks.json` — the public keys edge-bff verifies with<br>`/v1/internal/grants` — role/scoping grant for self-serve restaurant onboarding (system-role callers only; not in the edge allowlist) |

## Catalog — MS, :8002

Owns what a restaurant sells, and the entire menu cache pipeline.

| | |
|---|---|
| Postgres | **`catalog_db`** — `restaurants`, `restaurant_cuisines` (many-to-many tag rows, lowercase slugs), `menu_categories` (name, sort order, item membership), `menu_items` (+ modifier groups/options, `item_tags` filter tags), promo rules, tax tables, `outbox` (`restaurants.version` bumps in the same tx as the rows, category edits included); search via `tsvector` + `pg_trgm` GIN indexes maintained in the same tx (ADR-0019) |
| Calls | Identity — `POST /v1/internal/grants` on self-serve onboarding (grant `restaurant_admin` + `restaurant_id` to the creating user; idempotent, retried; never edge-routed). Onboarding is idempotent by owner — phase-1 claim model allows **one restaurant per user**, enforced by `UNIQUE(owner_user_id)`; a repeat POST returns the existing restaurant and re-attempts the grant (the repair path) |
| Redis | `catalog:menu:{rid}` — rendered menu, cache-aside: `DEL` on every menu-edit commit, refilled on read, 5-min TTL bounds the refill race (ADR-0027)<br>`catalog:browse:<gh5>:<cuisine>:<page>` — browse pages, 60s<br>`catalog:lock:menu:{rid}` — singleflight on render miss |
| Kafka | **produces** `catalog.changes` (compacted, outbox → Debezium) |
| Internal | `GET /v1/internal/restaurants/{rid}/snapshot?item_ids=…` — authoritative pricing **read** for Order's pricing lib (system-only, never edge-routed, cache-bypassing, torn-read-safe; persists nothing — the durable pricing snapshot lives in `order_db`) |
| CDN | menus (near-fresh, 5s), browse pages (30s), images |
| λ | — (the menu cache invalidates by `DEL` from the service; no async warmers) |

## Cart — no backend (client state, ADR-0017)

The cart is **not a service**. The FE persists it locally (item IDs, quantities, modifiers, browsed menu version — never prices). Two server-side pieces support it:

| | |
|---|---|
| Estimate | `POST /v1/quote` on **Order** — stateless, runs the same `smartfood-pricing` code as `PriceOrder`, so the reviewed price and the charged price come from one implementation |
| Placement | `POST /v1/orders` accepts IDs + quantities only; everything is re-resolved server-side at placement (`PRICE_CHANGED` diff on drift) |

Accepted losses: cross-device sync, survival across app-data clear, server-side abandoned-cart signal. Revisit triggers in [ADR-0017](adr/0017-cart-is-client-side.md).

## Inventory — MS, :8005

| | |
|---|---|
| Postgres | **`inventory_db`** — `stock` (the conditional-decrement row), `restaurant_load` (capacity counter: conditional increment `WHERE active < capacity` — kitchen slots as stock), `reservations` ledger + expiry reaper, `outbox` |
| Redis | `inventory:adm:<rid>:<sku>` — per-item admission bucket shielding the PG row from viral-item stampedes. **Advisory only; Postgres is the arbiter.** |
| Kafka | **produces** `inventory.events` (outbox → Debezium) |

## Order — MS, :8006

The state machine owner, and the only writer of order transitions.

| | |
|---|---|
| Postgres | **`order_db`** — `orders` (with `delivery_address_snapshot`) + `order_items` (per-line name/price snapshots — an order survives menu edits; ADR-0018) + `pricing_snapshot`, hour-partitioned `outbox` (partitions dropped ≤6h after publish confirmed), restaurant order feed index `(restaurant_id, status, placed_at)` |
| DynamoDB | none — it reads nothing from the tracking or history tables; those belong to the projectors |
| Redis | `order:idem:<key>` — idempotency fast-path, 15 min (PG keeps 7d and remains the arbiter) |
| Kafka | **produces** `orders.events` (outbox → Debezium) |
| Temporal | **starts** `OrderWorkflow` (`ord::<order_id>`) |
| Embeds | `smartfood-pricing` — the `PriceOrder` activity runs in-process in Order workers |

Expected first candidate for its own Aurora cluster under ADR-0016's split trigger; cell-prefixed ULIDs already make that a routing change.

## Payment — MS, :8007

| | |
|---|---|
| Postgres | **`payment_db`** — double-entry `ledger` (append-only, 7y), `idempotency` table (read-before-execute, money keys `<order_id>:<op>`), forward-compat PSP columns `psp`/`payment_intent_id`/`capture_before` + webhook-dedupe table shape (mock-populated until a real PSP; ADR-0010 amendment), `outbox` |
| Redis | `payment:idem:<key>` — fast-path only |
| Kafka | **produces** `payments.events` (outbox → Debezium) |
| External | mock PSP behind the `PaymentGateway` port ([ADR-0010](adr/0010-mock-psp-behind-payment-gateway-port.md)) |

## Dispatch — MS, :8009

The only DynamoDB-owned domain service, which is why its event path differs from everyone else's.

| | |
|---|---|
| DynamoDB | **`sfo-dispatch-deliveries`** (delivery-keyed + GSI on rider)<br>**`sfo-dispatch-rider-state`** — the conditional-write assignment lock, sole authority preventing double-assignment |
| Redis | **reads** `shared:geo:<cell>:<gh4>` for candidate search, `shared:loc:…`, `shared:hb:…` |
| Kafka | **produces** `dispatch.events` via DynamoDB Streams → forwarder λ (its outbox equivalent)<br>**produces/consumes** `rider.status` |
| Calls | OSRM for ETA matrices |

## rider-gateway — GW, :8010

Terminates rider WebSockets. Highest write rate in the system.

| | |
|---|---|
| DynamoDB | **`sfo-rider-locations`** — day-bucketed breadcrumbs, TTL 30d |
| Redis | **sole writer** of `shared:geo:<cell>:<gh4>` (GEOADD per ping), `shared:loc:<cell>:<rider_id>` (30s), `shared:hb:<cell>:<rider_id>` (90s); publishes to `shared:trk:<delivery_id>` every 2nd ping |
| Kafka | **produces** `rider.locations` (0.2 Hz sample) and `rider.status` — telemetry exception below |
| Protocol | WebSocket, bidirectional: GPS up, delivery offers down |

## tracking-gateway — GW, :8011

Owns no data at all. Pure fan-out.

| | |
|---|---|
| DynamoDB | **reads** `sfo-order-tracking` for the snapshot on every connect and reconnect |
| Redis | subscribes `shared:trk:<delivery_id>`; `GETDEL shared:ticket:<rand>` on connect |
| Kafka | none |
| Fallback | Temporal workflow query (rate-limit-guarded) when the read model lags >60s |
| Protocol | SSE, one-way push |

## Notification — MS, :8008

*(As built, W3: consumer-only durable inbox — this supersedes the planned rows this section carried (:8009, DDB `sfo-notification-log`, `dispatch.events`, RabbitMQ/λ sender fan-out); dedupe is the notification's natural key, no `processed_events` table.)*

| | |
|---|---|
| Postgres | **`notification_db`** — `notifications` (per-recipient inbox: `id ntf_<deterministic uuid5 of event_id+recipient>` — replay-safe natural-key dedupe; `recipient_type` customer\|restaurant, `recipient_id`, `order_id`, `kind`, `title`, `body`, `created_at` = event `occurred_at`, `read_at` nullable) + `order_recipients` projection (`order_id` PK, `user_id`, `restaurant_id` — upserted from every order event, because payment events carry no `user_id`) |
| Kafka | **consumes** `c1.orders.events` + `c1.payments.events` — one `EventConsumer` loop per topic, groups `notification.inbox.orders` / `notification.inbox.payments` (separate loops: a payments backoff must not block the orders loop that feeds the recipients projection); **produces** nothing |
| Serves | `GET /v1/notifications` (keyset cursor + unread count), `POST /v1/notifications/{id}/read` (ownership-in-WHERE, not-yours = 404), `POST /v1/notifications/read-all` — via edge, auth mode |

## Read-model projectors — W

| | |
|---|---|
| DynamoDB | **owns `sfo-order-tracking`** (TTL after terminal) and **`sfo-order-history`** (`PK=<customer>, SK=<ts>#<order>`) |
| Kafka | **consumes** `orders.events`, `dispatch.events` |
| Guarantee | version-guarded writes — a late `OrderConfirmed` after `OrderCancelled` no-ops. Rebuildable by replay; lag SLO p99 <2s, alert at 10s |

## Analytics — MS, :8012

| | |
|---|---|
| Postgres | **`sfo-aurora-analytics`** (dedicated cluster) — aggregate tables, 5s micro-batch upserts |
| Kafka | **consumes** every topic |
| S3 | raw lake via MSK Connect S3 sink; Firehose + λ for curated transforms |

## Workers

| Component | Owns | Notes |
|---|---|---|
| Temporal workers — W | no data of their own; Temporal state lives in `sfo-aurora-temporal` | Poll task queues, run activities by calling services. `PriceOrder` runs in-process here |
| Celery workers — W | no data | Consume RabbitMQ: receipts, thumbnails, reports, cache warming. Loss-tolerant only |

---

## The `shared:` keys — deliberate exceptions

Four Redis key families cross a service boundary. Each is intentional, and each has exactly one writer — which is why they carry the `shared:` prefix instead of an owner's.

| Key | Writer | Reader | Why it's allowed |
|---|---|---|---|
| `shared:geo:<cell>:<gh4>` | rider-gateway | Dispatch | A shared spatial index. Routing 30k pings/s through Dispatch's API to keep it private would add a hop to the hottest write path for no correctness gain. Dispatch falls back to the `rider_state` GSI if it is cold. |
| `shared:loc:<cell>:<rider_id>` | rider-gateway | Dispatch, tracking-gateway | Same reasoning; pure telemetry, 30s TTL. |
| `shared:hb:<cell>:<rider_id>` | rider-gateway | Dispatch (liveness sweeper) | Expiry *is* the signal — a key vanishing drives reassignment. |
| `shared:trk:<delivery_id>` | rider-gateway | tracking-gateway | Pub/sub channel; a broadcast has a publisher and subscribers by definition. |
| `shared:ticket:<rand>` | edge-bff | tracking-gateway (`GETDEL`) | A deliberate handoff — edge-bff authenticates, tracking-gateway redeems. Opaque and single-use. |

Everything else is private to its owner, enforced by `cache_client` namespacing plus Redis ACL key patterns.

## Outbox exception — telemetry topics

`rider.locations` and `rider.status` are **produced directly to Kafka by rider-gateway**, not through an outbox. This is a sanctioned exception to the "no service writes Kafka directly" rule (§1 of [ARCHITECTURE.md](ARCHITECTURE.md)), for a specific reason: the outbox exists to close the dual-write gap between *database state* and *an event about that state*. A GPS ping has no database state it must be atomic with — it is telemetry, sampled and loss-tolerant by design (we already drop 4 of every 5 pings). There is no inconsistency for an outbox to prevent.

The rule stands unchanged for every topic carrying a domain fact: orders, payments, inventory, catalog, identity, and dispatch.
