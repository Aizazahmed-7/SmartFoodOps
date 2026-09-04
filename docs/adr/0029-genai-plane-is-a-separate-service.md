# 0029 — The GenAI plane is a separate service; the ordering path never awaits an LLM

**Status**: Accepted (2026-09-02)

## Context

Part A shipped hooks for Part B and promised they were free: `partb.embeddings` on
`catalog.changes` (FR-54, FR-12), `partb.features` on `orders.events`, "the SSE fleet is
reused for Part B streaming LLM responses with zero changes" (ADR-0006), "Part B triggers"
as a Lambda verdict (ADR-0008), "pause analytics / **Part B consumers**" as shed step 2
(ADR-0014), and a Kafka fan-out budget that already counts Part B feeds (capacity-plan A10).
So the question was never *whether* to build the AI layer, but **where it lives and what it
is allowed to block**.

An LLM call has a failure profile unlike anything in Part A. Latency is seconds, not the
milliseconds every existing budget is written in. Availability is a third party's. The real
capacity ceiling is a **provider quota in tokens per minute**, not CPU — at the 2,500
orders/s ceiling, 5% engagement × 3 turns is ≈375 generations/s ≈ ~50M tokens/min, orders of
magnitude past any single account. Output is non-deterministic. Cost per call is ~1,000× a
Postgres read. Those are the properties of an external payment provider, not of a domain
service, and ADR-0010 already established how this codebase holds such a thing at arm's
length.

Three placements were considered. **Inside Catalog** is superficially attractive — Catalog
owns menus and already owns the `SearchPort` seam ADR-0019 built for exactly this trigger —
but it would put an LLM SDK, a vector index and a token budget inside the service that the
money path calls for pricing snapshots, and Catalog's p99 is load-bearing for checkout.
**Two services** (retrieval and generation split from day 1) is the shape this will eventually
want, since index size and LLM concurrency scale on different axes — but it doubles the ops
surface immediately, on a dev box that already OOMs at 7.7 GB, to buy a boundary a port
provides for free.

## Decision

1. **One new service: `services/ai-assistant/`, package `ai_assistant`, port 8013** (debugpy
   9013). 8003/8004 stay deliberately unused (ADR-0015, ADR-0017) and 8011 stays reserved for
   tracking-gateway. It is **cell-scoped** (`c1`) alongside Order and Analytics, not
   global-plane like Catalog: it reads cell-plane order data, and its index is geo-partitioned
   per city anyway, so a second cell gets a second index rather than a shared one.

2. **Three runtimes, one codebase**, mapping onto the three Part A already runs — and split by
   ADR-0025's rule (*replay-safe state ⇒ Kafka consumer; human-visible side effect ⇒ task
   queue*): the FastAPI app serves chat, streaming and internal retrieval; `EventConsumer`
   loops project `catalog.changes` into embeddings and `orders.events` into features; Celery
   workers run batch generation, reindex and profile recompute. An embedding upsert is a
   projection. A published promo blurb is a side effect.

3. **It owns `assistant_db`** and reaches every other service's data the way the ownership
   rule requires — that service's API or that service's topic, never its tables and never its
   Redis keys.

4. **No Part A service makes a synchronous call to `ai-assistant` on any write path.** The
   single read-path exception is Catalog's `SearchPort` hybrid adapter (ADR-0032), which is
   flag-gated, timeout-bounded, and **falls back to `PostgresSearch`** — search degrades from
   semantic to lexical, it does not fail.

5. **Retrieval and generation stay co-located** behind `retrieval/` and `generation/` inside
   the service, each behind its own ports. **Split trigger**: retrieval graduates to its own
   service when index QPS needs to scale independently of generation concurrency, or when the
   corpus outgrows one Postgres cluster — a deployment change, because the boundary is already
   a port.

6. **Failure is a 503, never a cascade.** `LlmUnavailable` maps to `DEPENDENCY_UNAVAILABLE`
   with `Retry-After`; the assistant sits on no path whose failure can cancel an order, void an
   authorization, or strand a delivery. ADR-0014's ladder is amended in the same change so the
   AI plane sheds *above* analytics — it is the first thing to go, not the second.

## Consequences

**Positive**

- The LLM's latency, quota and non-determinism are quarantined behind one service boundary and
  one set of ports; nothing in the saga, the ledger or the dispatch lock can be slowed or
  failed by a provider outage.
- Part A's promise survives largely intact: embeddings, streaming transport, telemetry and
  shedding all attach through hooks that already exist. The audit of where it does *not* hold
  is in the Part B PRD, stated rather than quietly papered over.
- One service means one deployment, one metrics job and ~600 MB in compose — `make up-ai`
  stays runnable beside the rest of the stack on this box.
- Cell-scoping inherits ADR-0013's invariants free: a second cell gets its own assistant and
  its own index, with no cross-city retrieval to unpick.

**Negative**

- Retrieval and generation share a scaling unit until the split trigger fires; a burst of chat
  traffic and a burst of search traffic contend for the same tasks.
- A second Postgres extension (`vector`) enters the shared cluster, and the AI service reads
  from four other services over HTTP — four more failure modes to hold ports and timeouts
  around.
- "No synchronous call on a write path" is a review-enforced rule, not a compile-time one. The
  layer-contracts scan can prove no *import* crosses; it cannot prove no HTTP call does.

**Revisit trigger**: retrieval QPS or corpus size firing the split trigger in §5; an AI
feature that genuinely needs to block a write (none is planned — every current use case is
advisory); or the assistant appearing in an incident's causal chain for an order that failed.
