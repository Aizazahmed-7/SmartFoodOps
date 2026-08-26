# 0026 — Dispatch's truth lives in DynamoDB; its events are copies

**Status**: Accepted

## Context

The dispatch milestone made ADR-0011 real: `rider_state`'s conditional write is the assignment lock, `deliveries` is the per-order state machine, and the whole service is the fleet's first with **no PostgreSQL at all**. That broke an assumption every other producer relies on: the transactional outbox (ADR-0002) exists to make a Kafka event atomic with the SQL write that made it true — and dispatch has no SQL write. Meanwhile analytics wants `RiderAssigned`/`RiderDeliveryCompleted` facts (FR-43's utilization), and the rider-gateway wants to publish downsampled GPS. The question: how do a DDB-truth service and a connection-plane gateway speak Kafka honestly?

## Decision

1. **DynamoDB is the truth; Kafka carries copies.** `dispatch.events` and `rider.locations` are **direct-produced** (`send_nowait`, no-raise, no outbox) — the browse-telemetry posture, for the same structural reason: there is no transaction for an outbox row to join. A dropped event costs an analytics data point, never an assignment: every consumer decision (who is locked, who owns a delivery) reads DDB through a conditional write, never a topic.
2. **Event identity stays deterministic** (`uuid5(type:aggregate:marker)`) so downstream PK-dedupe absorbs whatever redelivery the direct path does produce.
3. **The prod path is DynamoDB Streams → Kafka**, closing the at-most-once gap the way Debezium closes it for SQL: the WAL-equivalent (the stream) is the single write. Recorded as the named upgrade, not built — dev's direct produce keeps the loop readable and LocalStack-cheap.
4. **The workflow, not a daemon, is dispatch's clock.** Offer expiry, the pickup deadline, and the no-rider deadline are Temporal timers in DeliveryWorkflow; dispatch's HTTP surface only converts and reads locks. Consequence: every timing race resolves through one conditional write plus one workflow read (`expire` answering `already_assigned` is the lost-accept self-heal), and there is no scanner process to operate. The 60s SCAN reconciliation of FR-32 (belt-and-braces for a dead workflow) stays a named deferral.
5. **Courier facts enter the saga through order's internal API** — dispatch never touches Temporal, the kitchen precedent: a service signals only workflows it owns.

## Consequences

**Positive**
- Zero new stateful infrastructure beyond the tables ADR-0007 already planned; the lock authority and the state machine live in one place with one consistency model.
- Every failure mode lands in an existing pattern: lost event → deterministic-id dedupe or nothing; lost signal → the workflow's next timer reads DDB; dead broker → riders keep working over REST.
- The gateway stays stateless (connections + relay), scaling on ADR-0006's axis.

**Negative**
- Dev's `dispatch.events` is at-most-once until DDB Streams: a Kafka blip under-counts utilization (accepted for telemetry-grade facts; the DDB rows remain queryable truth).
- Dispatch decisions cost DDB round-trips per cascade step — fine at dev scale, and ADR-0011 already priced the ceiling (~3.2k conditional writes/s inside on-demand floors).
- Two spellings of the Redis geo keys (gateway writes, dispatch reads) with cross-referenced comments — the layer contract forbids the import that would unify them; drift is loud (candidates vanish) but possible.
