# 0013 — Multi-region deferred; cell-ready invariants kept from day 1

**Status**: Accepted

## Context

The design ceiling (5–10k orders/s peak) eventually implies multiple cells and regions, but launch traffic does not, and multi-region distributed-systems machinery (global routing, replicated Aurora, Kafka mirroring, Temporal failover) is the most expensive thing to build speculatively. The user explicitly deferred it. The risk to manage is not missing multi-region — it is accidentally building something that *cannot* become multi-region without a re-architecture.

## Decision

**Part A ships one region, one cell** (`c1`) — full write path + data planes — sized at 2,000 orders/s sustained, 2,500 provisioned. Single-region HA via multi-AZ: Aurora multi-AZ, MSK 3 brokers across AZs, ECS spread across AZs.

Four near-zero-cost invariants keep the multi-cell door open, enforced from day 1:

1. Order/delivery IDs are **cell-prefixed ULIDs** with shard bits reserved — any tier can route by ID forever.
2. All config, Kafka topic names, Redis keyspaces, and Temporal namespaces are **parameterized by `cell_id`** (currently one value).
3. **No cross-city joins or queries anywhere** — enforced in review; the invariant that makes cells possible.
4. The Dispatch assignment lock stays **region-local — never a DDB Global Table** (LWW replication would silently break mutual exclusion; see ADR-0011).

Explicitly deferred, documented not designed: additional cells, routing global table, Aurora Global, MSK Replicator, Temporal cross-region failover, RTO/RPO targets, game-days.

## Consequences

**Positive**
- No speculative infrastructure, replication lag, or conflict-resolution semantics to operate before traffic demands them.
- When Phase 4 arrives, adding a cell is a deployment change (stamp `c2`, add routing), not a migration — the invariants guarantee it.

**Negative**
- A regional outage is a full outage in Part A (multi-AZ covers AZ loss only) — accepted at launch scale.
- The invariants cost ongoing review vigilance; one cross-city join merged casually erodes the guarantee.

**Revisit trigger**: sustained traffic at 60% of the cell write budget (scale runbook threshold), or a business availability requirement exceeding single-region SLOs → execute Phase 4.
