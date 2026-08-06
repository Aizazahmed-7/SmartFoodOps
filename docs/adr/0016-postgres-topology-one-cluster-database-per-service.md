# ADR-0016 — Postgres topology: one cluster, one database per service

**Status**: Accepted

## Context

"Database per service" is stated everywhere in this design but was never pinned down, and the documents disagreed: the deployment view showed a single `Aurora PG` node, local dev creates one database per service inside one container, while the capacity plan named "Order Aurora," an Analytics Postgres, and a Temporal persistence cluster as separate things.

The ambiguity matters because the two readings buy different things:

- **One cluster, database per service** gives **schema ownership** — no service can read another's tables, migrations are independent, the boundary is enforced by grants. It gives **no performance isolation**: shared CPU, IOPS, buffer pool, and connection budget.
- **One cluster per service** adds performance isolation, at the cost of N multi-AZ clusters to provision, patch, back up, monitor, and pay for.

Teams routinely say the first and then reason as if they had bought the second. That mistake was made in this project's own review of the Cart datastore choice, which is what surfaced the gap.

## Decision

**Phase 1 runs one Aurora PostgreSQL cluster (`sfo-aurora-main`) containing one logical database per service**, each with its own role and no cross-database grants. Applications connect through RDS Proxy.

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

Splitting is deliberately cheap: because each service already owns a separate logical database with no cross-database queries, moving one is a dump, restore, and connection-string change — not a data model change.

## Consequences

**Positive**
- Schema ownership and independent migrations from day 1, which is the property that actually prevents coupling.
- One cluster to provision, patch, monitor, and pay for at launch traffic, instead of five.
- Local dev matches production structurally: one container, one database per service, same grants.
- The split trigger is observable (per-database write metrics already tagged `svc`), so graduation is a decision, not a surprise.

**Negative**
- **No performance isolation between services in phase 1.** A pathological query in Catalog can raise latency in Order. Mitigations: `statement_timeout` per role, per-database connection caps in RDS Proxy, and per-database write-rate alerts that fire before the split trigger.
- The shared connection budget is a real ceiling; RDS Proxy is mandatory, not optional.
- Arguments about workload isolation must now be made honestly — "it's a separate database" does **not** mean "it cannot affect you." Any design reasoning that depends on isolation must say *cluster*.

**Revisit trigger**: any item in the split-trigger list, or a second team taking ownership of a service.
