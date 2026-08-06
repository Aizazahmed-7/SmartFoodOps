# 0012 — Dual-mode outbox publisher (`OUTBOX_MODE=poller|debezium`)

**Status**: Accepted

## Context

ADR-0002 makes Debezium the only production path to Kafka — but Kafka Connect + Debezium costs 1–1.5 GB RAM and slow startup, hostile to the 16 GB-laptop slim-mode dev loop (`make up` core ≈ 3 GB). If developers bypass the outbox locally because CDC is too heavy, local behavior diverges from production exactly where correctness lives.

## Decision

The shared `smartfood-outbox` library ships two publishers behind one switch:

| `OUTBOX_MODE` | Mechanism | Where |
|---|---|---|
| `poller` (dev default) | In-process asyncio poller: `SELECT … FOR UPDATE SKIP LOCKED`, same topic/key/Avro output, same at-least-once + per-aggregate ordering | local slim mode |
| `debezium` | Kafka Connect + Debezium tailing the outbox (compose `cdc` profile locally; MSK Connect in AWS) | CI, staging, prod |

**Both modes emit byte-identical records** — envelope, Avro encoding, keys, headers (including the `traceparent` lift). Parity is enforced by CI always running `debezium` mode, plus the nightly chaos job that kills Connect mid-run and asserts the outbox drains with no gaps or reorders. The same pattern covers LocalStack's unreliable DDB Streams triggers: **`DISPATCH_FORWARDER=poller|lambda`** (dev: boto3 shard-iterator poller).

## Consequences

**Positive**
- Developers run the true outbox path all day at ~zero RAM cost; the transactional-outbox invariant is never "skipped locally".
- CI on `debezium` means the production mechanism is exercised on every merge, not first met in staging.
- Consumers cannot tell modes apart, so no test needs mode-conditional logic.

**Negative**
- Two publisher implementations to keep semantically identical — the parity guarantee is a maintained artifact, backed by CI, not a given.
- The dev poller adds slight publish latency and per-service polling load (irrelevant at dev volumes).

**Revisit trigger**: a Debezium feature the poller cannot mirror byte-identically (e.g., a new SMT), or record-parity CI failing twice in a quarter → re-evaluate whether the poller earns its maintenance cost.
