# 0010 — Mock PSP behind hexagonal PaymentGateway port

**Status**: Accepted

## Context

Part A must exercise real payment semantics — authorize, capture, void, refund, and every failure mode — without a live PSP contract. The dangerous payment bugs live in the failure paths (timeouts, unknown outcomes, retries that double-charge), which a happy-path stub never exercises and a real sandbox exercises non-deterministically.

## Decision

Payment integrates through a **hexagonal `PaymentGateway` port** (`authorize/capture/void/refund`); phase 1's only adapter is a **mock PSP service** (compose port 9080) with failure injection: probabilistic env knobs (`DECLINE_RATE`, `TIMEOUT_RATE`, `UNKNOWN_OUTCOME_RATE`, `LATENCY_MS_P50/P99`) plus **magic card tokens** (`tok_decline`, `tok_timeout`, `tok_unknown`) for deterministic tests. `UNKNOWN` outcomes later fire a webhook, exercising reconciliation locally.

The correctness machinery around the port is real, not mock: double-entry ledger (append-only, 7y retention, `ledger_imbalance_cents ≠ 0` alerts), idempotency table read-before-execute with money keys `{order_id}:{op}` — an `AuthorizePayment` retry after an unknown outcome can never double-charge. Auth and refund amounts are computed only from the immutable pricing snapshot.

## Consequences

**Positive**
- CI proves the invariants a sandbox can't: chaos suite asserts N injected timeouts still yield ≤ 1 authorization per order, and a `TIMEOUT_RATE=1.0` window compensates every affected workflow.
- Swapping in Stripe/Adyen later is one adapter behind the port; ledger, idempotency, saga compensation, and webhooks are already production-shaped.
- `order-gen --card-mix "ok:0.9,tok_decline:0.05,tok_timeout:0.05"` makes failure paths part of everyday local dev, not a special rig.

**Negative**
- The mock encodes our *model* of PSP behavior; real PSPs add quirks (partial captures, async declines, idempotency-window limits) the adapter layer must absorb later.
- No real PCI/compliance surface is exercised in Part A — deliberately out of scope.

**Revisit trigger**: signing a real PSP → build the adapter against the same port and run the identical chaos suite against its sandbox; the mock stays forever as the CI failure-injection harness.
