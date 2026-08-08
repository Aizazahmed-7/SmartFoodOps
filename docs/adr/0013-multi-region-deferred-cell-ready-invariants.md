# 0013 — Multi-region deferred; cell-ready invariants kept from day 1

**Status**: Accepted — amended by [ADR-0018](0018-v2-review-register.md) (region rule; multi-cell mechanism pre-specified)

## Context

The design ceiling (5–10k orders/s peak) eventually implies multiple cells and regions, but launch traffic does not, and multi-region distributed-systems machinery (global routing, replicated Aurora, Kafka mirroring, Temporal failover) is the most expensive thing to build speculatively. The user explicitly deferred it. The risk to manage is not missing multi-region — it is accidentally building something that *cannot* become multi-region without a re-architecture.

## Decision

**Part A ships one region, one cell** (`c1`) — full write path + data planes — sized at 2,000 orders/s sustained, 2,500 provisioned. Single-region HA via multi-AZ: Aurora multi-AZ, MSK 3 brokers across AZs, ECS spread across AZs.

Four near-zero-cost invariants keep the multi-cell door open, enforced from day 1:

1. Order/delivery IDs are **cell-prefixed ULIDs** with shard bits reserved — any tier can route by ID forever.
2. All config, Kafka topic names, Redis keyspaces, and Temporal namespaces are **parameterized by `cell_id`** (currently one value).
3. **No cross-city joins or queries anywhere** — enforced in review; the invariant that makes cells possible.
4. The Dispatch assignment lock stays **region-local — never a DDB Global Table** (LWW replication would silently break mutual exclusion; see ADR-0011).

Explicitly deferred: additional cells, Aurora Global, MSK Replicator, Temporal cross-region failover, RTO/RPO targets, game-days. (Originally "documented not designed"; ADR-0018 upgraded the mechanism — cell map, plane split, activation runbook — to *designed, not built*: see the amendment below. Activation remains gated on this ADR's revisit trigger.)

## Consequences

**Positive**
- No speculative infrastructure, replication lag, or conflict-resolution semantics to operate before traffic demands them.
- When Phase 4 arrives, adding a cell is a deployment change (stamp `c2`, add routing), not a migration — the invariants guarantee it.

**Negative**
- A regional outage is a full outage in Part A (multi-AZ covers AZ loss only) — accepted at launch scale.
- The invariants cost ongoing review vigilance; one cross-city join merged casually erodes the guarantee.

**Revisit trigger**: sustained traffic at 60% of the cell write budget (scale runbook threshold), or a business availability requirement exceeding single-region SLOs → execute Phase 4.

## Region selection (binding at Phase 3) — ADR-0018

Chosen when the first production cell is provisioned, and binding from then on: a cell's region is **in-country if AWS has one, else the nearest region; always 3 AZs; avoid us-east-1 for the cell**. Nothing before Phase 3 depends on the choice (local dev and CI are region-free), which is exactly why it can be deferred to the moment it becomes real.

## Mechanism (pre-approved, not built) — ADR-0018

Recorded so that "adding a cell is a deployment change, not a migration" is falsifiable rather than aspirational. **Activation stays gated on this ADR's revisit trigger** (60% of the cell write budget sustained, or the business expansion/availability case); nothing below is built before it fires.

**Plane split.** Global plane — one instance, lives alongside `c1` until cell 3: Identity, Catalog, the edge cell-router, `sfo-global-cell-map`, CloudFront/Route53. Cell plane — stamped per cell: Order, Payment, Inventory, Dispatch, both gateways, projectors, Notification, analytics ingest, the Temporal namespace (or cluster, post-ADR-0009 migration), Kafka, Redis (one cluster today; the ADR-0018 D5 cluster groups if that split has fired), the per-service databases, DDB tables (suffixed `-c2`), OSRM.

**Cell map.** `sfo-global-cell-map` — a DynamoDB global table holding routing metadata only (never the assignment lock; invariant 4 stands): `city_code → {cell_id, region}`.

**Assignment rule.** An order lives in its restaurant's cell; a rider works exactly one cell. Nothing replicates between cells except the global plane — Aurora Global and MSK Replicator stay out because cells are independent by design.

**Activation runbook (5 steps).**

1. CDK-instantiate the cell stack with `cell_id=c2` (+ region per the clause above).
2. Stamp the names invariant 2 already parameterizes: topics `c2.*`, the Temporal namespace, DDB `-c2` suffixes, Redis keyspaces — pure config, no code.
3. Map new cities → `c2` in the cell map; migrating an existing city = flip its restaurants' `cell_id` in a low-traffic window — in-flight orders complete in `c1`, because cell-prefixed IDs (invariant 1) route by prefix forever.
4. Edge cell-router honors the map.
5. Cell-isolation game-day before real traffic.

**Multi-region-within-country** is this same runbook with a different region parameter — no additional mechanism required.
