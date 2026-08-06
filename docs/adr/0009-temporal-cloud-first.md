# 0009 — Temporal Cloud first, self-host on EKS at cost tripwire

**Status**: Accepted

## Context

Temporal is correctness-critical (ADR-0001) and operationally deep: history shards, persistence tuning, matching-service scaling, upgrades. Self-hosting well requires expertise the team should not be building while also building the product. Temporal Cloud's per-action pricing is cheap at launch traffic and expensive at ceiling (~40 actions/order).

## Decision

**Phase 1 runs Temporal Cloud** (workers on ECS Fargate; per-cell namespaces; locally, the Temporal dev server with SQLite persistence). **Migrate to self-hosted Temporal on EKS when sustained load exceeds ~200–300 orders/s** — the point where per-action pricing crosses the cost of a properly-run cluster. The migration is pre-planned, not improvised: cost-per-order dashboards (CUR → Athena → Grafana, `svc`+`cell` tags from day 1) make the tripwire observable, and worker code is identical against Cloud and self-hosted — only endpoint/mTLS config changes.

Capacity note for the self-hosted target: ≥4k history shards at the 2.5k orders/s cell ceiling (shard count is fixed at cluster creation — sized for the ceiling, not launch).

## Consequences

**Positive**
- Zero Temporal ops burden during the phase where the team is proving out saga design; upgrades, scaling, and persistence are someone else's pager.
- The decision is reversible by construction — an infra swap, not a code change; workers, workflows, and task queues carry over.
- Spend is proportional to traffic exactly when traffic is small.

**Negative**
- At ceiling, per-action pricing would be a large recurring cost — acceptable only because the tripwire fires long before.
- The EKS migration reintroduces the operational depth we deferred (and our only EKS footprint — everything else is Fargate); it must be scheduled deliberately, with load tests, when the tripwire fires.
- External dependency for the most correctness-critical component; mitigated by fail-closed 503 posture (degraded, never corrupt).

**Revisit trigger**: sustained load > 200–300 orders/s, or cost-per-order dashboards showing Temporal Cloud spend crossing the modeled self-hosted cost — whichever fires first.
