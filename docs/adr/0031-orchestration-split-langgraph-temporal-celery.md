# 0031 — Orchestration split: LangGraph owns the turn, Temporal owns sagas, Celery owns batch

**Status**: Accepted (2026-09-02)

## Context

This codebase already has a settled, defended answer to "who orchestrates". ADR-0001 gives
the order saga to Temporal. ADR-0025 splits everything else in one line: *replay-safe state ⇒
Kafka consumer; human-visible side effect ⇒ task queue.* Anti-pattern #26 bans ad-hoc retry
loops and `tenacity` outright, precisely so that "where did this retry" has exactly one answer
per code path.

Part B's mandated stack introduces LangGraph, which is **also** an orchestrator. It ships
retries, checkpointers, durable state, interrupts and resumption — a substantial overlap with
Temporal's job, arriving through a dependency rather than a design review. Adopting it
uncritically would give the fleet two orchestrators with two retry policies, two notions of
durable state, and two places to look when something ran twice. Rejecting it outright would
mean hand-rolling the branching a real assistant turn needs, and would ignore a stack the
brief mandates.

The honest question is narrower than "LangGraph or not": **what does an assistant turn
actually need orchestrating, and is that the same problem Temporal solves?** It is not. A turn
is seconds long, in-process, side-effect-free, and disposable — if it fails, the user asks
again. A saga is minutes-to-hours long, spans services, has money in it, and *must* compensate
rather than be retried by a human. Those want different machinery, and the mistake would be
to let one tool's presence imply it should do the other's job.

## Decision

**LangGraph owns exactly one thing: the control flow of a single assistant turn.** It earns
that because a turn genuinely branches — intent classification selects which retrievers run,
menu/vector, order-context and taste-profile retrieval fan out in parallel, and tool-calling
needs a bounded loop. Expressing that as a graph is clearer than expressing it as nested
`if`/`await`, and the parallel fan-out is where the latency budget is actually won.

**What LangGraph does not own**, each delegated to the thing that already owns it:

| Concern | Owner | Reference |
|---|---|---|
| Retries | the adapter, bounded, exponential + jitter | ADR-0030 §6, anti-pattern #26 |
| Durability / resumption | **nothing** — checkpointer stays off | see below |
| Cross-service consistency | Temporal | ADR-0001 |
| Scheduling, batch, side effects | Celery over RabbitMQ | ADR-0025 |
| Replay-safe projection | `EventConsumer` | ADR-0021, ADR-0025 |
| All I/O | our ports | ADR-0029, ADR-0030, ADR-0032 |

**The checkpointer stays off, deliberately.** A half-finished turn is not resumed; it is
abandoned and the user re-asks. The durable record is the `messages` row and its
`message_chunks` — written by us, in our schema, with our migration story — not a
framework-owned state blob whose format is a dependency's private business. This also keeps
the turn free of the one thing that would make it a workflow: nothing in it needs
compensating, because nothing in it has an external effect.

**LangGraph does not touch I/O.** Nodes call our `LlmPort`, `EmbeddingPort`, `VectorStore` and
the platform read ports — never LangChain's provider wrappers, retrievers or vector-store
integrations. Three reasons, all load-bearing here: the layer-contracts scan requires
`domain/` to stay framework-free and testable headless; pyright's strict tier is unsatisfiable
across a dependency with patchy `py.typed` unless it is held behind our own `Protocol`s; and
the 100% coverage gate is only reachable if every branch can be driven by a scripted fake.
The graph therefore lives in `domain/graph/` and runs with no app, no network and no key.

**The turn runs as a bounded background asyncio task in the API process** — not a Celery job,
not a Temporal workflow. ADR-0008's rule decides it: request-shaped and latency-sensitive ⇒
containers, and a broker hop would land directly in the time-to-first-token budget, which is
the one number a user feels. Concurrency is capped by a semaphore sized from the provider
quota (ADR-0030), so an assistant burst cannot starve the event loop that also serves the
retrieval API. The task is decoupled from the HTTP connection on purpose — that is what makes
mid-stream reconnect resumable (ADR-0034).

**Batch generation rides Celery**: content drafts, feedback summaries, reindex, taste-profile
recompute. These are ADR-0025's other half — published copy is a human-visible side effect,
and a reindex is a long job wanting its own queue, its own retry schedule and its own DLQ.
**Embedding projection rides Kafka** (ADR-0033), because an idempotent upsert keyed by content
hash is a projection by definition.

**If an AI feature ever needs cross-service consistency, it does not get it from LangGraph.**
An assistant that places an order calls `POST /v1/orders` like every other client and the
saga owns it from there. The assistant never orchestrates a money path.

## Consequences

**Positive**

- One answer per concern survives Part B: retries are in adapters, sagas are in Temporal,
  batch is in Celery, projections are in Kafka. LangGraph is additive rather than a second
  opinion.
- The graph is a pure, headless, injectable object — every branch reachable from a scripted
  fake, which is the only way `fail_under = 100` and a non-deterministic dependency coexist.
- Turn latency pays no broker or workflow tax; time-to-first-token is bounded by the provider
  and our own retrieval, nothing else.
- Dropping the checkpointer means no framework-owned durable state to migrate when LangGraph
  changes its serialisation format.

**Negative**

- We give up LangGraph's resumption and human-in-the-loop interrupts. If a future feature
  wants a genuinely long-running, resumable agent (a multi-step booking, say), this ADR is the
  one to supersede — and Temporal, not the checkpointer, is the likely answer.
- Using ~30% of a framework invites drift: a well-meaning change importing
  `langchain_anthropic` would compile, pass pyright at standard tier, and quietly bypass the
  router, the budget breaker and the metrics. A source-scan test in the layer-contracts idiom
  bans LangChain integration imports outside `adapters/`.
- Turn concurrency shares a process with the retrieval API; the semaphore is the only thing
  between a chat burst and search latency. It is a tuned number, and tuned numbers rot.

**Revisit trigger**: an AI use case that must survive a process restart mid-execution, or that
needs true human-in-the-loop suspension across minutes; or turn concurrency contending
measurably with the retrieval API, which would move generation to its own process before it
moves it to a broker.
