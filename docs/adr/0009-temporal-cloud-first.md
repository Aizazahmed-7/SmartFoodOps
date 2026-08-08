# 0009 — Temporal Cloud first, self-host on EKS at cost tripwire

**Status**: Accepted — amended by [ADR-0018](0018-v2-review-register.md) (D1: tripwire arithmetic corrected)

## Context

Temporal is correctness-critical (ADR-0001) and operationally deep: history shards, persistence tuning, matching-service scaling, upgrades. Self-hosting well requires expertise the team should not be building while also building the product. Temporal Cloud's per-action pricing is cheap at launch traffic and expensive at ceiling (~40 actions/order).

## Decision

**Phase 1 runs Temporal Cloud** (workers on ECS Fargate; per-cell namespaces; locally, the Temporal dev server with SQLite persistence). **Migrate to self-hosted Temporal at Phase 3 production readiness, before sustained traffic** — the original "~200–300 orders/s" tripwire was an arithmetic error; the true Cloud/self-host crossover is ~10–30 orders/s sustained (worked math in the amendment below). The migration is pre-planned, not improvised: cost-per-order dashboards (CUR → Athena → Grafana, `svc`+`cell` tags from day 1) make the tripwire observable, and worker code is identical against Cloud and self-hosted — only endpoint/mTLS config changes.

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

**Revisit trigger**: superseded by the amendment below — the migration is now scheduled at Phase 3 production readiness rather than cost-triggered; cost-per-order dashboards remain as verification that the crossover math holds.

## Amendment (ADR-0018, D1) — corrected crossover arithmetic

The tripwire this ADR shipped with was wrong, and so was the reviewing document's counter-claim. Worked math at Temporal Cloud list price (~$25 per million actions), ~40 actions/order (the figure this ADR was written against):

- Cost per order ≈ 40 × $25/1M = **~$0.001/order**.
- At N orders/s sustained: N × 40 actions × ~2.6M s/month × $25/M ≈ **N × ~$2,600/month** — $26k/mo at 10 orders/s, ~$130k/mo at 50, ~$520–780k/mo at the old 200–300 tripwire.
- A properly-run self-hosted cluster (Temporal services + persistence + fractional ops attention) costs the equivalent of roughly $25–80k/month.
- **True crossover: ~10–30 orders/s *sustained*** — roughly 10× earlier than this ADR's original trigger, which would have fired only past ~$0.5M/month of avoidable spend. (v2's "$200k/mo by 50 orders/s" was itself 1.5–4× inflated; both sides had bad arithmetic. Corrected here per the register.)
- The halved action budget (~20/order per ADR-0018's Temporal budget row) halves cost-per-order and moves the crossover further out — helpful, but it does not resurrect the old tripwire.

**Corrected plan**: self-host at **Phase 3 production readiness, *before* sustained traffic** — a planned migration executed with slack, never a cost-triggered scramble under load.

**Drain-migration runbook** (the property that makes the move cheap): workflow lifetimes are <2 h, so persistence never migrates —

1. Stand up the new cluster (history shards ceiling-sized per the capacity note above).
2. Point **new workflow starts** at the new cluster — endpoint/mTLS config swap; worker code is identical.
3. Let the old cluster drain naturally (~2 h for order workflows).
4. Decommission after a 24 h observation window.

A routing change, not a data migration.

**Build phase unaffected**: local dev runs the Temporal dev server (SQLite persistence) throughout; nothing in the build plan moves.
