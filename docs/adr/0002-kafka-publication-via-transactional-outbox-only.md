# 0002 — Kafka publication via transactional outbox only

**Status**: Accepted

## Context

Events must be independent, traceable, recoverable, and never double-processed. The classic failure is the dual write: a service commits to its DB, then fails to publish (or publishes without committing) — state and stream diverge silently, and downstream projections, analytics, and Part B feeds go quietly wrong.

## Decision

**No service writes Kafka directly.** Every state change commits its event to a transactional `outbox` table in the same DB transaction as the state change; **Debezium (MSK Connect)** tails the outbox and publishes to Kafka. Dispatch, whose store is DynamoDB, publishes via **DDB Streams → a forwarder Lambda** emitting the identical envelope. Envelope: Avro via Schema Registry (`BACKWARD_TRANSITIVE`, CI compatibility gate) with `event_id` (UUIDv7 dedupe key), `event_type`, `aggregate_id`, `aggregate_version`, `occurred_at`, `cell_id`; `traceparent` stored as outbox columns and lifted into Kafka headers by a Debezium SMT, so the async hop stays trace-stitched.

Outbox tables are hour-partitioned; partitions are dropped ≤6h after publish is confirmed (never row-deletes at 20k rows/s).

## Consequences

**Positive**
- The dual-write gap is structurally closed: an event exists iff its state change committed. At-least-once delivery plus `event_id`/`aggregate_version` idempotent consumers yields end-to-end effectively-once.
- One envelope, one publication path — projectors, analytics, and Part B consumers need no per-producer special cases.
- Kafka replay can rebuild read models from a truthful log (a runbook, not a hope).

**Negative**
- Publication adds latency (outbox → CDC → Kafka); budgeted at `outbox_publish_lag_seconds` p99 < 5s, alerted.
- Debezium/Connect is heavy operationally and locally — mitigated by the dual-mode publisher (ADR-0012).
- Two publication mechanisms (Debezium for PG, Streams forwarder for DDB) must be kept envelope-identical; enforced by shared `smartfood-kafka` serde and CI parity tests.

**Revisit trigger**: outbox publish lag persistently breaching 5s p99 at the 2,500 orders/s ceiling, or a store that offers native transactional publish with equivalent guarantees.
