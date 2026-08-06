# 0001 — Temporal owns the order saga (vs choreography)

**Status**: Accepted

## Context

An order is a distributed transaction spanning pricing, inventory reservation, payment authorization, restaurant acceptance, and dispatch, with timers (3-min accept window) and multi-step compensation. Pure event choreography scatters this control flow across consumers, making "where is this order and what happens next?" a forensic exercise, and makes compensation ordering emergent rather than declared.

## Decision

One Temporal `OrderWorkflow` (`workflow_id = ord::{order_id}`, `REJECT_DUPLICATE`/`USE_EXISTING` so duplicate submits attach to the running execution) owns each order's saga: control flow, timers, restaurant-decision signals, the `DeliveryWorkflow` child (`ParentClosePolicy=REQUEST_CANCEL`), and reverse-order compensation. The workflow is the **single writer** of order transitions; every transition is guarded (`UPDATE … WHERE status='prev'`, 0 rows = no-op) so retries are safe.

Division of labor, enforced in review: **Temporal decides what happens next** (saga control flow, timers, compensation); **Kafka tells everyone what already happened** (facts); **Celery does chores** (loss-tolerant work). Litmus test: if losing or double-running a message could corrupt an order or money, it must be a Temporal activity or an outbox-published Kafka event — never a bare REST call or Celery task.

## Consequences

**Positive**
- Saga logic is one readable function; compensation order is explicit (void/refund → release reservation → cancel), retried forever with 5-min cap, alert at 10 attempts, page at 1h — never silently dropped.
- Deterministic replay makes workflow decisions effectively exactly-once; money math is computed only from the immutable pricing snapshot, so replays are safe.
- Temporal Web + `workflow_id=order_id` search attribute gives per-order forensics for free.

**Negative**
- Temporal becomes a correctness-critical dependency (top risk #1): mitigated by per-cell namespaces, 3× load tests, fail-closed 503 (degraded, never corrupt).
- Workflow determinism rules (no direct I/O, versioned changes) are a real learning curve for the team.
- ~40 Temporal actions/order at 2.5k orders/s drives the ADR-0009 cost tripwire.

**Revisit trigger**: Temporal p99 schedule-to-start latency violating the placement SLO (p99 PLACED→CONFIRMED < 6s) under 3× load test, or an order-lifecycle change that no longer fits a single-writer saga.
