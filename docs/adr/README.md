# Architecture Decision Records — SmartFoodOps Part A

Source of truth for *why* the architecture is the way it is. Each ADR is short (Context / Decision / Consequences) and carries an explicit revisit trigger. The design plan and `docs/ARCHITECTURE.md` describe *what*; these records pin the *why* so future changes argue against the original reasoning, not folklore.

| # | Title | Status |
|---|---|---|
| [0001](0001-temporal-owns-the-order-saga.md) | Temporal owns the order saga (vs choreography) | Accepted |
| [0002](0002-kafka-publication-via-transactional-outbox-only.md) | Kafka publication via transactional outbox only | Accepted |
| [0003](0003-custom-fastapi-edge-bff.md) | Custom FastAPI edge-bff instead of an API gateway product | Accepted |
| [0004](0004-http-json-internal-grpc-deferred.md) | HTTP/JSON for internal calls, gRPC deferred | Accepted |
| [0005](0005-jwt-verified-once-at-edge.md) | JWT verified once at edge; identity headers + network trust internally | Accepted |
| [0006](0006-sse-for-customers-websocket-for-riders.md) | SSE for customer tracking, WebSocket for riders; ticket auth for SSE | Accepted |
| [0007](0007-dynamodb-partition-key-rules.md) | DynamoDB partition-key rules: uniform-cardinality only | Accepted |
| [0008](0008-serverless-verdicts-lambda-for-bursty-edges.md) | Serverless verdicts: Lambda for bursty loss-tolerant edges only | Accepted |
| [0009](0009-temporal-cloud-first.md) | Temporal Cloud first, self-host on EKS at cost tripwire | Accepted |
| [0010](0010-mock-psp-behind-payment-gateway-port.md) | Mock PSP behind hexagonal PaymentGateway port | Accepted |
| [0011](0011-dispatch-assignment-lock-via-ddb-conditional-writes.md) | Dispatch assignment lock via DynamoDB conditional writes | Accepted |
| [0012](0012-dual-mode-outbox-publisher.md) | Dual-mode outbox publisher (`OUTBOX_MODE=poller\|debezium`) | Accepted |
| [0013](0013-multi-region-deferred-cell-ready-invariants.md) | Multi-region deferred; cell-ready invariants kept from day 1 | Accepted |
| [0014](0014-load-shedding-ladder.md) | Load-shedding ladder: admission at edge, money path never shed | Accepted |
| [0015](0015-pricing-is-a-library-not-a-service.md) | Pricing is a shared library, not a service | Accepted |
| [0016](0016-postgres-topology-one-cluster-database-per-service.md) | Postgres topology: one cluster, one database per service | Accepted |
| [0017](0017-cart-is-client-side.md) | Cart lives on the client, not the backend | Accepted |
| [0018](0018-v2-review-register.md) | v2 review register: adoptions, triggers, rejections | Accepted |
| [0019](0019-search-postgres-fts-first-opensearch-behind-port.md) | Search: Postgres FTS + trigram first, OpenSearch behind a port later | Accepted |
| [0020](0020-onboarding-consistency-outbox-convergence-not-temporal.md) | Onboarding consistency: sync grant + outbox convergence, not Temporal | Accepted |
| [0021](0021-consumer-failure-policy-bounded-retry-then-dlq.md) | Consumer failure policy: supervised loop, bounded retry, then DLQ | Accepted |
| [0022](0022-roles-as-seeded-lookup-table.md) | Roles as a seeded lookup table, pinned to the enum | Accepted |
| [0023](0023-placement-runs-inside-the-order-workflow.md) | Placement runs inside the order workflow (update-with-start), sweeper retired | Accepted |
| [0024](0024-orders-row-is-placements-idempotency-record.md) | The orders row is placement's idempotency record; the key table retired | Accepted |
| [0025](0025-side-effects-ride-a-task-queue.md) | Side effects ride a task queue; projections ride the log | Accepted |
| [0026](0026-dispatch-truth-in-dynamodb-events-as-copies.md) | Dispatch's truth lives in DynamoDB; its events are copies | Accepted |

**Conventions**: files are `NNNN-kebab-title.md`; numbers are never reused. Superseding an ADR = new ADR + status change here, never editing the old decision.
