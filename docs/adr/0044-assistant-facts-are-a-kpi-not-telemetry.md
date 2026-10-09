# 0044 — Assistant interaction facts are a KPI, not telemetry

**Status**: Accepted (2026-09-21)

## Context

FR-95 names six metrics the product is measured on: assistant usage,
questions asked vs answered, order conversion after an AI interaction,
recommendation acceptance rate, average AI response time, and customer
engagement. All six are counts or averages over one event: what happened in
a single turn.

There is already a cheaper-looking path for this. `c1.browse.events` carries
`MenuViewed` fire-and-forget, no outbox, explicitly lossy — "telemetry has
no write to be atomic with" (flows.md). Copying that here is tempting: an
assistant turn is *also* an interaction nobody pays for directly, and the
publish would never touch the request path.

It is the wrong shape, and the reason is what the numbers are FOR. A browse
count that is 2% low changes nobody's decision. "Questions answered" and
"conversion after AI interaction" are the numbers this milestone is
justified by — a KPI that quietly drops events is a KPI that reports
improvement when the bus is flapping, and there is no way to tell from the
number itself.

The turn also has something to be atomic with, which `MenuViewed` does not:
the `messages` row that records the answer. If the fact and the row can
disagree, the store and the dashboard disagree.

## Decision

1. **Through the outbox, like every other business fact** (ADR-0002). The
   fact is staged in the same transaction that settles the message it
   describes, and drained by the ordinary poller/Debezium path to
   `c1.assistant.events`, keyed by `message_id`.

2. **The staging site is guarded, and the guard is the message's own
   status.** `finish_message` became a guarded transition
   (`WHERE status = 'streaming'`) that reports whether it won; only the
   winner stages. This is ADR-0035's rule — a replayed emit is harmless
   because the caller loses its aggregate's guard before it ever reaches
   `stage_event` — and without it "exactly one fact per answer" would rest
   on nobody calling the function twice, which is a hope, not an invariant.
   Every downstream query is a `COUNT`.

3. **Absolute values, keyed by a natural id.** The fact says what the
   interaction WAS — its outcome, its cited ids, its duration — never "+1
   question". The outbox is at-least-once and the poller re-sends anything
   it published but did not mark, so a delta-shaped fact would inflate the
   KPI every time a poller died between those two steps.

4. **A failure is a fact.** `outcome` is one of `answered | refused |
   no_match | failed`, and all four are published. A metric that counted
   only successes would improve every time the provider got worse — and
   `refused` vs `no_match` must stay distinguishable, or a broken retriever
   hides behind a safety policy working correctly.

5. **The cited restaurant ids travel with the fact.** FR-97 joins
   interaction → order on `(user_id, restaurant_id)`, and the turn is the
   only place that mapping is free: the answer's markers carry item ids, and
   resolving those to restaurants afterwards is a join against a menu that
   has since changed. Only the restaurants actually CITED are recorded, not
   everything retrieved — a conversion credited to a restaurant the customer
   was never shown is an invented number.

6. **The question text is not in the payload.** A KPI needs counts and ids.
   Shipping what a customer typed into an analytics store puts NFR-32's
   retention rule on a second copy nobody remembers to purge — the same
   reasoning that keeps identities out of prompts (ADR-0043 §6).

## Consequences

**Positive**

- The dashboard cannot be quietly wrong. A fact that is staged is a fact
  that will arrive, and one that arrives twice converges.
- `duration_ms` measures the whole turn including retrieval, which is the
  wait a customer actually sits through. A provider-only number would
  flatter us, and it is the flattering one that is easy to collect.
- Analytics needs no assistant-specific transport: it is another consumer
  group on another cell-prefixed topic, with the envelope it already parses.

**Negative**

- A row per turn in `assistant_db`. Small, but it is a second thing the
  90-day purge has to cover, and it does not cascade from a conversation —
  the outbox is flat by design. (This originally read "small next to
  `message_chunks`"; that table was dropped with FR-69, so the outbox row is
  now the only per-turn write besides the message itself.)
- Staging inside the settle transaction makes that transaction do two
  writes on the answer's critical path. It is off the request path (the turn
  is detached), so the cost lands on the turn's tail rather than on a
  customer's wait.
- The fact's shape is now a contract. Analytics projects it in B7, and a
  field removed later is a dashboard that breaks at a distance.
- `outcome` is derived from the graph's `stopped` field, so a new
  short-circuit edge in the turn is a new outcome value that nothing forces
  anybody to handle downstream.

**Revisit trigger**: a second AI surface (recommendations in B4, drafts in
B6) needing facts of a different shape — the question then is one topic with
a discriminator or a topic each; or interaction volume reaching a point
where the per-turn row is worth batching.
