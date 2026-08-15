# ADR-0018 — v2 review register: adoptions, triggers, rejections

**Status**: Accepted

## Context

Two external review documents ("Final Architecture & Build Plan v2.2" and "Engineering Handbook v1.2") proposed 14 architecture deltas plus ~40 engineering practices against our v1 baseline. Every difference was evaluated adversarially on two axes: technical merit at the 2,500 orders/s ceiling (checking their arithmetic), and fit for this project's ratified constraints (mock PSP, HTTP/JSON internal, deferred multi-region, the 3-week build plan, small team, mandated stack). 54 items: 21 adopted, 16 partially adopted, 11 recorded as trigger-gated Plan Bs, 6 rejected.

This ADR is the register. Individual amendments live in the affected ADRs/docs; this is the index and the reasoning of record.

## Adopted into the baseline (design now; implementation per build plan)

| Item | What changed | Where |
|---|---|---|
| Payment gate rename | `PAYMENT_AUTHORIZED` → **`PAYMENT_CLEARED`**, method-agnostic (`orders.payment_method ∈ {CARD, COD}`, Part A always CARD via mock PSP); Kafka event names unchanged (brief-mandated) | ARCHITECTURE §6, PRD |
| Payment wait sub-states | 3DS-analog/PSP-outage waits are a `payment_wait_reason` column on `VALIDATED`, never new machine states | ARCHITECTURE §6.2 |
| Order snapshots (v1 gap) | `order_items` table (name/price snapshots per line) + `delivery_address_snapshot` on orders; addresses get soft-delete — an order must survive menu edits and address deletion | ARCHITECTURE §9, service-ownership |
| Deterministic event identity (v1 gap) | `event_id` = UUIDv5 of `aggregate:{id}:{version}:{type}`; outbox emit conditional on the guarded transition applying; no SR/network calls inside the tx | ARCHITECTURE §11, smartfood-outbox |
| Per-sink dedupe modes | PG_TX / VERSION_GUARD / NATURAL_KEY declared per consumer — universal `processed_events` would be ~100k needless inserts/s at ceiling | ARCHITECTURE §11 |
| Saga-sweeper | Commit-then-start-workflow gap closed by a consumer of our own `OrderPlaced` events (W3) *(As built, W3: a periodic DB scan in the order API process, not a consumer — a heal must retry forever, and the ADR-0021 consumer runtime would park it to the DLQ on a Temporal outage; see ARCHITECTURE §6 as-built note)* | ARCHITECTURE §6 |
| Kafka/SR conventions | **RecordNameStrategy** subjects (TopicRecordNameStrategy would fork every subject per cell prefix); topics cell-prefixed `c1.*` from day 1; consumer-group `.v{n}` naming | ARCHITECTURE §11 |
| CDC operating spec | Per-DB Debezium connector/slot/publication (PG-forced), heartbeats, `max_slot_wal_keep_size`, slot-loss-on-failover runbook (outbox-scoped re-snapshot; gap-free because partition drops gate on confirmed publish) | ARCHITECTURE §11, ADR-0016 |
| Realtime plane params | Own subdomain + dedicated ALB, WS/SSE never traverse CloudFront (no caching value; origin-timeout friction; blast radius — *not* "CF breaks SSE"); connection lifetime **uniform-random 15–30 min** (fixed lifetime = recurring reconnect waves) | ADR-0006, ARCHITECTURE, capacity-plan |
| Dispatch time bounds (v1 gap) | Arrive-by timer (ETA+5 min → unassign+re-offer), unassigned READY >10 min → auto-cancel via compensation, post-PICKED_UP heartbeat loss >5 min → at-risk ops queue, never auto-cancel | ARCHITECTURE §8 |
| Temporal action budget | ≤12 activities + ≤3 timers + ≤4 signals happy-path (≈20 actions/order, was ~40): NotifyRestaurant becomes an `OrderConfirmed` consumer, PriceOrder is a local activity, transitions fold into owning activities. Replay suite counts commands (warn W2, gate at Phase 3) | capacity-plan, ARCHITECTURE §6 |
| Multi-cell mechanism | Global-plane vs cell-plane split, cell map schema, assignment rule (order→restaurant's cell), 5-step activation runbook — **activation still trigger-gated per ADR-0013**; "deployment change, not re-architecture" is now falsifiable | ADR-0013 |
| Region rule | In-country else nearest region, 3 AZs, avoid us-east-1 for the cell | ADR-0013, ARCHITECTURE §13 |
| Backups & DR (v1 gap) | RPO ≤5 min / RTO ≤4 h; PITR everywhere incl. Temporal persistence; cross-region snapshot copies; SR `_schemas` export; quarterly restore game-day — "a backup never restored is a hypothesis" | ARCHITECTURE §13 |
| PgBouncer over RDS Proxy | asyncpg prepared statements pin RDS Proxy sessions, defeating multiplexing; transaction-mode PgBouncer per service; RDS Proxy kept for Lambdas only | ADR-0016 |
| Money rules | Only Payment imports the PSP adapter; integer minor units end-to-end; no client-asserted amounts (review rules) | ADR-0010 |
| Engineering practices | Error envelope + code catalog, DTO strictness, idempotency-key semantics (reserve-first, body-hash), tx-boundary rules, query/migration hygiene, API inventory, DoD + anti-pattern checklists, observability naming, security rules | docs/api-standards.md, docs/engineering-checklists.md, repo-structure |

## Trigger-gated Plan Bs (recorded, not baseline)

| Item | Trigger → pre-approved action |
|---|---|
| Self-hosted Temporal (D1) | **ADR-0009 arithmetic corrected**: true Cloud/self-host crossover is ~10–30 orders/s *sustained* (v2's "$200k/mo at 50/s" is 1.5–4× inflated, but our old 200–300/s tripwire was worse). Plan: migrate at Phase 3 production readiness, *before* sustained traffic. Drain-migration property (<2 h workflow lifetimes) makes it a routing change. Build phase: local dev server, unaffected |
| Three-way Redis split (D5) | Keyspace→cluster-group mapping documented now (money/realtime/catalog); `cache_client` resolves endpoint per namespace. Split at Phase 3 ceiling provisioning or on correlated-degradation evidence |
| ClickHouse analytics (D7) | `analytics_db` stands for the build; ClickHouse at ingest-lag SLO breach or >5k events/s sustained |
| EKS + Istio (D8) | ECS Fargate remains the documented Phase 3 target *unless* the D1 self-host trigger fires first — self-hosted Temporal makes EKS the coherent substrate; decide as one package then |
| gRPC internal (D9) | ADR-0004 stands (user-ratified; v2's case is organizational and depends on the D8 mesh). Trigger: multi-team ownership or the EKS move → protobuf/`buf` per v2 spec; generated HTTP clients make later conversion mechanical |
| Stripe + payment-webhook (D3/D14) | Mock PSP stands (user decision; deterministic chaos CI depends on it). Forward-compat adopted now: `psp`/`payment_intent_id`/`capture_before` columns, webhook-dedupe table shape. Trigger per ADR-0010: real-PSP contract signed |
| COD (D4) | Strongest new design in v2 (single-lock headroom decrement, system-set amounts, recon invariant) but rests on a market requirement our PRD doesn't contain. Trigger: product adds COD → adopt v2 §4.3 design wholesale |
| Runtime flag system, PG prod posture, AWS ops pack (SASL/SCRAM, SES, gateway pools) | Each recorded with its phase trigger in the affected doc |

## Rejected

| Item | Why |
|---|---|
| Python 3.13 | Locked on 3.12 across workspace + images; churn without benefit mid-build |
| Repo/naming churn (`sfo_` prefixes, port remaps, top-level `workflows/`) | Contradicts built, tested code; zero functional gain |
| Real-Stripe-as-baseline | See D3 trigger — sequencing disagreement, not architecture |
| COD-machinery-as-baseline | See D4 trigger |
| K8s deployment baseline (HPA/PDB/probes spec) | Follows D8; adopt with it if its trigger fires |
| v2 bootstrap sequencing | Conflicts with the user-ratified 3-week plan, which achieves the same safety via outbox-rows-from-day-1 |

**Verdict of record**: v2 is our architecture reviewed well — its self-corrections (deterministic event IDs, order snapshots, dispatch bounds, backups) found real v1 gaps and are adopted; its re-platforming (EKS/gRPC/Stripe/day-1-self-hosting) optimizes for a large funded team at launch scale and is trigger-gated, not baseline. Where v2's arithmetic was wrong (D1) we corrected it *and* our own.
