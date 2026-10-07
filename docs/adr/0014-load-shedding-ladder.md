# 0014 — Load-shedding ladder: admission control at edge, money path never shed

**Status**: Accepted — amended by [ADR-0029](0029-genai-plane-is-a-separate-service.md) (the AI plane sheds at step 2a, above analytics)

## Context

Meal-time spikes and viral restaurants will exceed provisioned capacity; the question is what fails, in what order, decided by whom. Uncontrolled overload fails randomly — timeouts mid-saga, half-written orders — which violates the brief's "partial failures must not corrupt state". Degradation must be a designed sequence, not an emergent one.

## Decision

**Admission control at the edge**: per-cell token buckets (Redis) sized at 1.5× load-tested capacity. Over budget → **429 before any state is written** — an order is either admitted and will complete (or compensate cleanly), or it never existed. Redis-down fallback is a per-pod local limiter.

**The money path is queued, never shed.** Admitted orders ride the Temporal backlog — the sanctioned buffer; placement latency stretches, but no admitted order is dropped, and inventory/payment invariants hold.

Degradation ladder (steps 1–4 automated, 5–6 ops-approved):

| # | Step | Trigger class |
|---|---|---|
| 1 | CDN serves stale browse pages | automated |
| 2a | AI generation → retrieval-only (templated explanations, cached recommendations, lexical+vector search; no LLM call) | automated |
| 2b | Pause analytics / Part B consumers (incl. the embedding projection) | automated |
| 3 | GPS sampling 0.2 → 0.05 Hz | automated |
| 4 | Tracking cadence 2s → 5s | automated |
| 5 | Serve stale menu cache | ops-approved |
| 6 | Restaurant capacity gating | ops-approved |

**Amendment (ADR-0029)**: step 2 splits because the AI plane and the analytics plane are not
the same sacrifice. **2a** costs a minority of users a nicer sentence — search, recommendations
and delay explanations all still answer, from the vector index, the taste profile and the
template cache respectively — while freeing the single most expensive per-request resource in
the fleet. It therefore sheds *before* the telemetry that the dashboards driving this very
ladder are computed from. **2b** is the original step, now explicitly including the embedding
projection, whose lag costs only menu-freshness in retrieval.

The AI plane additionally sheds on **its own** signal, independently of this ladder: the
per-cell spend/quota circuit breaker in [ADR-0030](0030-llm-providers-behind-a-port.md) §5
drives the same 2a degrade when the provider — not the platform — is the thing under strain.
Two triggers, one degrade path, so there is only ever one degraded mode to reason about.

## Consequences

**Positive**
- Overload degrades read freshness, generated prose and telemetry — never order or money
  correctness; every shed step is loss-tolerant by construction.
- 429-before-write means rejected requests need no cleanup, no compensation, no orphaned state.
- The ladder is rehearsable: each step has a known cost, owner, and rollback, instead of ad-hoc incident improvisation.

**Negative**
- Queuing instead of shedding the money path means p99 placement latency degrades under sustained overload before anything breaks — the SLO dashboard, not errors, is the early signal.
- Steps 5–6 visibly degrade product experience (stale menus, restaurants gated) and directly reduce revenue — hence human approval.

**Revisit trigger**: any incident where the ladder fired out of order, a shed step corrupted state, or Temporal backlog drain time exceeded the placement SLO budget → re-sequence the ladder or resize admission buckets.
