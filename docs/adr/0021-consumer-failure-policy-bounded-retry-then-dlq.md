# 0021 — Consumer failure policy: supervised loop, bounded retry, then DLQ

**Status**: Accepted

## Context

Identity (grant convergence) and inventory (stock provisioning) each hand-rolled the same aiokafka consume loop — duplicated at-least-once config, duplicated traceparent handling, and a shared failure mode: an exception escaping the consume task killed it **silently**, and the service kept serving HTTP while its consumer was dead. Neither loop had retry or DLQ handling — a poison message either crashed the task or wedged redelivery on one partition indefinitely. Meanwhile [repo-structure.md](../repo-structure.md) has promised "typed consumer framework, retry/DLQ" from `smartfood-kafka` since W2, unfulfilled. W3 raises the stakes: the notification inbox is the **first consumer of `c1.orders.events` and `c1.payments.events`** — order-volume topics where a stuck partition is a user-visible outage, not a background inconvenience.

## Decision

**All consumers run on `EventConsumer` in `libs/smartfood-kafka`** (`smartfood_kafka/consumer.py`): the shared at-least-once loop — decode → handle → **commit-after-handle**, aiokafka auto-commit hardcoded OFF, `auto_offset_reset="earliest"`, multi-topic subscribe, traceparent-header extraction + structlog `trace_id` rebinding. A consumer is a handler registration, not a loop. Failure policy, in escalation order:

1. **Supervised loop.** A crashed consume pass logs an error and rejoins after `restart_seconds` (default 5s), mirroring `OutboxPoller.run`. Silent task death is eliminated as a mode.
2. **Bounded in-process retries** on handler exceptions: `max_attempts` (default 5) with exponential backoff from base 0.5s (0.5/1/2/4s ≈ 7.5s horizon).
3. **DLQ park.** On exhaustion — or immediately for an undecodable message — the ORIGINAL raw bytes are produced to **`<source-topic>.dlq`** (e.g. `c1.orders.events.dlq`; key preserved, original headers preserved) with failure-metadata headers: `dlq.error.type`, `dlq.error.message` (truncated 500), `dlq.source.topic`/`dlq.source.partition`/`dlq.source.offset`, `dlq.attempts`, `dlq.failed_at`. Then the offset is **committed** and the partition keeps moving. A failed DLQ publish leaves the offset uncommitted → supervised restart → redelivery — at-least-once holds through the failure of the failure path.

DLQ topics are broker-auto-created and uncompacted. **Replay contract**: inspect via the Kafka console (:8085); replay = re-produce the raw DLQ value to the source topic — it is byte-identical to the original, and consumer dedupe (deterministic `event_id`s, ADR-0002) absorbs anything already handled. `smartfood_kafka.testing` (StubKafkaConsumer, StubSerde, RecordingHandler, StubDlq, StubMessage) ships alongside so every consumer's failure branches are unit-testable.

## Consequences

**Positive**
- A poison message costs seconds, not a partition: ~7.5s of retry, one DLQ produce, commit, move on.
- Silent consumer death is gone — the supervised loop logs and rejoins, same shape as the outbox poller.
- The at-least-once configuration (auto-commit off, commit-after-handle, earliest) is written and tested **once** in the lib; the per-service `pragma: no cover` aiokafka blocks collapse into covered lib code.
- Every future consumer (notification is the template) is a handler registration on a tested runtime.

**Negative**
- A sustained dependency outage — anything beyond the ~8s retry horizon — drains events to the DLQ at retry-horizon pace; recovery is a manual replay until tiered retry topics exist.
- Bounded in-process backoff blocks the partition for up to ~8s per failing event.

**Revisit trigger**: the first real outage-drain, or when the observability slice lands consumer-lag/DLQ-depth alerting — then tiered retry topics (`retry.5s`/`1m`/`10m` re-publish) replace in-process backoff.
