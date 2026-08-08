# ADR-0016 — Postgres topology: one cluster, one database per service

**Status**: Accepted — amended by [ADR-0018](0018-v2-review-register.md) (pooling: PgBouncer, not RDS Proxy; CDC topology)

## Context

"Database per service" is stated everywhere in this design but was never pinned down, and the documents disagreed: the deployment view showed a single `Aurora PG` node, local dev creates one database per service inside one container, while the capacity plan named "Order Aurora," an Analytics Postgres, and a Temporal persistence cluster as separate things.

The ambiguity matters because the two readings buy different things:

- **One cluster, database per service** gives **schema ownership** — no service can read another's tables, migrations are independent, the boundary is enforced by grants. It gives **no performance isolation**: shared CPU, IOPS, buffer pool, and connection budget.
- **One cluster per service** adds performance isolation, at the cost of N multi-AZ clusters to provision, patch, back up, monitor, and pay for.

Teams routinely say the first and then reason as if they had bought the second. That mistake was made in this project's own review of the Cart datastore choice, which is what surfaced the gap.

## Decision

**Phase 1 runs one Aurora PostgreSQL cluster (`sfo-aurora-main`) containing one logical database per service**, each with its own role and no cross-database grants. Applications connect through per-service **PgBouncer in transaction mode** (amended by ADR-0018 — see *Connection pooling* below; RDS Proxy is kept for Lambdas only).

| Database | Owner |
|---|---|
| `identity_db` | Identity |
| `catalog_db` | Catalog |
| `inventory_db` | Inventory |
| `order_db` | Order |
| `payment_db` | Payment |

**Two workloads already sit on their own clusters**, because their access patterns are genuinely unlike OLTP and unlike each other:

| Cluster | Why separate |
|---|---|
| `sfo-aurora-analytics` | Continuous 5s micro-batch upserts plus long analytical reads from Grafana — would pollute the OLTP buffer pool |
| `sfo-aurora-temporal` | Temporal's persistence has its own shard-count and write-amplification profile, and it is correctness-critical (ADR-0001) |

**Split trigger** — a service graduates to its own cluster when *any* holds:

- It exceeds **~30% of the cluster's write budget** (Order is the expected first mover; ADR-0013's cell-prefixed ULIDs already make its split a routing change).
- Its access pattern degrades neighbours — sustained sequential scans, long transactions, or vacuum pressure visible in other databases' latency.
- It needs a different availability or retention posture (Payment's 7-year ledger is the likely case).
- Its **CDC blast radius** outgrows the shared cluster (ADR-0018): replication slots live on the cluster writer, so one database's stalled or lost slot retains WAL for everyone on it. A service whose outbox volume or failover profile repeatedly presses the `max_slot_wal_keep_size` bound graduates — moving the database moves its slot's blast radius with it.

Splitting is deliberately cheap: because each service already owns a separate logical database with no cross-database queries, moving one is a dump, restore, and connection-string change — not a data model change.

## Consequences

**Positive**
- Schema ownership and independent migrations from day 1, which is the property that actually prevents coupling.
- One cluster to provision, patch, monitor, and pay for at launch traffic, instead of five.
- Local dev matches production structurally: one container, one database per service, same grants.
- The split trigger is observable (per-database write metrics already tagged `svc`), so graduation is a decision, not a surprise.

**Negative**
- **No performance isolation between services in phase 1.** A pathological query in Catalog can raise latency in Order. Mitigations: `statement_timeout` per role, per-service connection caps in each PgBouncer pool, and per-database write-rate alerts that fire before the split trigger.
- The shared connection budget is a real ceiling; pooling through PgBouncer is mandatory, not optional.
- Arguments about workload isolation must now be made honestly — "it's a separate database" does **not** mean "it cannot affect you." Any design reasoning that depends on isolation must say *cluster*.

**Revisit trigger**: any item in the split-trigger list, or a second team taking ownership of a service.

## Connection pooling (amended by ADR-0018)

The original decision mandated RDS Proxy. That was wrong for this stack: **asyncpg's named prepared statements pin RDS Proxy sessions**, defeating multiplexing exactly where the shared connection budget needs it most. Corrected posture:

- Each service runs its own **PgBouncer in transaction mode**; the service's `db_url` points at its PgBouncer, never at Aurora directly.
- asyncpg connects with `statement_cache_size=0` (unnamed prepared statements only — required for transaction-mode compatibility).
- **RDS Proxy is kept for Lambdas only**, where per-invocation connection churn is the problem it actually solves.
- `cl_waiting > 0` sustained on any pool is the saturation signal — pool/instance action, observed per service.

## CDC topology (ADR-0018)

How Debezium attaches to this cluster — one operating spec, recorded here because slots are a cluster-level resource:

- **One connector, one replication slot, one publication per logical database** (order, payment, inventory, identity, catalog). Postgres logical replication is per-database, so this is forced — the point is that nobody tries to share or multiplex.
- **Publications are scoped to the outbox tables only** (`table.include.list=<db>.outbox*`) — CDC is an outbox transport here, never a general table feed.
- `heartbeat.interval.ms=10000` with a heartbeat table write, so slots keep advancing on quiet databases instead of silently retaining WAL.
- Cluster parameter **`max_slot_wal_keep_size`** bounds how much WAL a stalled slot can hold hostage — the cap that keeps one database's CDC mishap from filling the shared writer's disk.
- **Slot loss on failover** (slots live only on the old writer): the recovery runbook — recreate the connector with an outbox-scoped snapshot, gap-free because partition drops gate on confirmed publish — lives in [ARCHITECTURE §11](../ARCHITECTURE.md); the graduation consequence lives in the split-trigger list above.
