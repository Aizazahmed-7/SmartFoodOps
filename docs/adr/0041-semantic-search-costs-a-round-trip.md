# 0041 — Semantic search is the primary path, and it costs a provider round trip

**Status**: Accepted (2026-09-21) — supersedes NFR-22's latency clause for the search path

## Context

ADR-0019 gave `/v1/search` a p99 budget of **150 ms**, and PRD Part B's NFR-22
restated it for hybrid retrieval: *"Semantic + hybrid retrieval p99 < 150 ms
at the ceiling — the same budget ADR-0019 set for lexical search, because
`/v1/search` may now route through it."*

That number was written before anything measured a real embedding provider.
B2 measured one, and the budget does not survive contact:

| | measured |
|---|---|
| Gemini `gemini-embedding-001` embed, one query | min 393 ms, **median 508 ms**, max 1066 ms |
| `/v1/search` end to end, semantic path | min 504 ms, **median 558 ms**, max 588 ms |
| SQL alone (both legs, fused) | ~50 ms |
| ADR-0019 / NFR-22 budget | **150 ms** |

The earlier "p99 91 ms" figure in the B2 walkthrough was taken with the
deterministic FAKE embedder — no network — so it measured the SQL and not
retrieval. It is corrected there.

The arithmetic is not close and cannot be tuned away. **Roughly 90% of a
semantic search is one HTTPS round trip to a hosted embedding model**, and
nothing in the query path is slow: the two legs plus RRF cost ~50 ms against
a warm index.

This was invisible until real credentials existed. With the fake embedder the
whole path ran in 91 ms and every measurement said the budget was met — and
worse, once the real provider was connected, every search silently exceeded
the 150 ms timeout and fell back to lexical while still returning 200s. An
eval run scored that fallback at 0.60 and reported it as the semantic path's
number. A budget that is enforced by a timeout does not fail loudly; it
fails by quietly serving the other system.

## Decision

1. **Semantic retrieval is the primary path for `/v1/search`, and the
   150 ms budget no longer applies to it.** The timeout becomes **2 s** —
   chosen to be comfortably above the measured max (1066 ms embed, 588 ms
   end to end) rather than tuned to a percentile, because its job is now to
   catch a *hung* provider, not to enforce a latency SLO.

2. **Lexical search remains the fallback and is not removed.** That was
   never a performance choice: FR-65 and ADR-0029 §4 permit this call at all
   only because killing the assistant degrades search rather than breaking
   it. A slow provider and a dead one are still different outcomes, and both
   still end at `PostgresSearch`.

3. **NFR-22's latency clause is superseded for the search path** and stands
   for everything else it governs. The honest restatement: *retrieval SQL*
   stays inside 150 ms; *retrieval including a provider embedding call* is a
   ~600 ms operation.

4. **The trade is accepted deliberately and is revisitable.** The product
   judgement is that a customer typing "something light" and waiting ~0.6 s
   for a dish that shares no words with the query is a better search than an
   instant one that cannot answer the question at all. B2 measured exactly
   that: `vague-light` went from recall 0.00 on the lexical/fake path to
   0.50 with real embeddings, surfacing Raita for "something light".

## Consequences

**Positive**

- Search answers questions lexical matching structurally cannot, which is
  the entire point of B2.
- The number is now measured and written down rather than assumed, and the
  measurement says precisely where the time goes — so any future work knows
  it is optimising a network call, not a query plan.
- The fallback is unchanged, so the resilience property ADR-0029 §4 required
  is untouched: assistant dead → lexical, still 200.

**Negative**

- `/v1/search` is **~11× slower** than it was. On a mobile connection that is
  a visible wait where there was none.
- The saving throw is a cache nobody has built. A query-embedding cache in
  Redis would make repeat phrasings fast and leave first-time queries at
  ~600 ms; it is the obvious next optimisation and is not in B2.
- A slow provider now costs 2 s before the fallback engages, where it
  previously cost 150 ms. Under a provider brown-out, search gets slow before
  it gets lexical.
- This is one more place where the system's behaviour depends on a third
  party's latency, which is exactly the property ADR-0029 quarantined the AI
  plane to avoid — and here it sits on a Part A read path.

**Revisit trigger**: a query-embedding cache landing (which changes the
distribution, not the worst case); measured customer abandonment on search;
a local or co-located embedding model, which would put the whole path back
inside 150 ms; or the B3 chat panel proving the better home for semantics, in
which case `/v1/search` can go back to lexical and this ADR is reversed
rather than amended.
