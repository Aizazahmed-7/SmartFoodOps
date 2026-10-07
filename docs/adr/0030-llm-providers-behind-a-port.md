# 0030 — LLM providers behind a port: task routing, cross-vendor failover, budget breaker

**Status**: Accepted (2026-09-02)

## Context

The Part B brief names the provider as a choice — "OpenAI / Groq / Anthropic" — which is a
requirement to make the choice reversible, not to make it. ADR-0010 already settled how this
codebase holds a third party at arm's length: a hexagonal port, declines as *results* and
transport failures as *exceptions*, and a swap that touches no caller. That pattern transfers
directly. Three properties of LLM providers demand more than a plain adapter on top of it.

**Tasks are heterogeneous by an order of magnitude.** Classifying an intent, rewriting a
query, and reranking fifty candidates are cheap, short, latency-critical calls. Generating a
grounded answer is none of those things. Sending all of them to one model overpays for the
small ones and underserves the large one; the difference is ~20× in cost and ~5× in latency,
per turn, forever.

**Quota is per-vendor and per-account, and it is the actual ceiling.** ADR-0029 does the
arithmetic: ~50M tokens/min at the provisioned order ceiling. No single account is provisioned
for that, so a single vendor is simultaneously a capacity wall and a single point of failure —
and provider incidents are correlated across *all* customers of that provider, which is
exactly the failure shape multi-AZ exists to avoid everywhere else in this system.

**Spend is unbounded by default.** Every other resource in Part A fails closed when
over-consumed: Redis evicts, the edge 429s, DynamoDB throttles. A retry loop against a
metered API does not fail closed — it bills. Cost per order is small at the modelled ratios
(~$0.0005), so the risk is not the steady state; it is a runaway context, a retry storm, or
an abusive client turning a product feature into an unbounded invoice.

## Decision

1. **`LlmPort` is a `Protocol` in `domain/`**, with `complete()` and `stream()`, and it keeps
   ADR-0010's discipline exactly: a refusal, a length truncation, a tool call and an empty
   answer are **results** (`Completion.finish_reason ∈ stop|length|refusal|tool_call`);
   `LlmUnavailable` and `LlmRateLimited` are the only exceptions, and they mean transport, not
   content. `EmbeddingPort` and `VectorStore` (ADR-0032) sit beside it under the same rule.

2. **Two adapters from day 1, because one is a ceiling.** `AnthropicLlm` (`claude-sonnet-5`
   for generation, `claude-haiku-4-5` for the cheap tier) and `OpenAiLlm` (failover generation,
   and `text-embedding-3-small` for `EmbeddingPort` — one SDK and one key covers both).
   Groq and Bedrock are additive: an adapter file and a router row, no caller changes.

3. **A `ModelRouter` policy object in `domain/` maps task → `ModelSpec`** (provider, model,
   max input tokens, max output tokens, timeout, budget class). The task vocabulary is closed
   and named — `CLASSIFY`, `REWRITE`, `RERANK`, `GENERATE`, `EXPLAIN`, `CONTENT_DRAFT`,
   `SUMMARIZE` — so "which model answers this" is one table, reviewable in one place, and
   hot-reloadable per cell the way dispatch scoring weights already are. Callers name a task;
   they never name a model.

4. **Failover is unconditional and safe.** On `LlmUnavailable` or `LlmRateLimited` the router
   tries the task's secondary provider once. This is sound in a way the PSP's is not: an LLM
   call has **no side effect at the provider**, so a duplicated call cannot double-charge
   anyone or leave an ambiguous outcome to reconcile. Money needed `PspUnavailable` to retain
   the same key; here there is nothing to retain.

5. **Three nested budget limits, all fail-closed.**
   - *Per request*: input tokens are counted **before** the call and the context is truncated
     to the `ModelSpec` cap. A prompt is never sent to find out how big it was.
   - *Per user*: a Redis token bucket at `assistant:budget:<sub>` (TTL always set — NFR-13),
     exceeded → 429 `RATE_LIMITED`. The edge's new `AI` rate-limit class bounds request rate;
     this bounds token spend, which request rate does not.
   - *Per cell*: a spend circuit breaker at `assistant:cb:<cell>`. Open ⇒ the plane degrades
     to **retrieval-only** wherever a non-generative answer exists (search, recommendations
     from the taste profile, templated explanations), and 503s only where it does not. A
     breaker that returns errors where it could return a worse-but-real answer is a worse
     breaker.

6. **Retries live in the adapter and nowhere else** — bounded attempts with exponential
   backoff and jitter, shaped like `MockPspClient._post`. No `tenacity`, no LangGraph
   checkpointer retry, no ad-hoc loop in a graph node (anti-pattern #26; ADR-0001 owns
   orchestration retries, ADR-0021 owns consumer retries, ADR-0025 owns task retries).

7. **The key is never defaulted.** `llm_api_key: str = ""` disarms the adapter — the same
   disarm convention as `redis_url=""`, `otlp_endpoint=""` and `kafka_consumers="off"` — so
   the unit suite needs no network and no credential, and a missing key in an environment is a
   loud 503 rather than a mystery. Real values arrive via an untracked
   `deploy/compose/.env` locally and Secrets Manager in deployment; `.env.example` carries a
   placeholder only.

8. **Every call is metered at the port boundary**: `assistant_tokens_total{model,direction}`,
   `assistant_response_seconds{stage,model,outcome}`,
   `assistant_time_to_first_token_seconds`. Cost is derived in Grafana from a price table, not
   computed in code — prices change on the vendor's schedule, not ours.

## Consequences

**Positive**

- Switching or adding a vendor is an adapter plus a router row. The brief's "OpenAI / Groq /
  Anthropic" stays an open question at runtime instead of a rewrite.
- Cheap work runs on cheap models by policy rather than by whoever wrote the node, and the
  policy is one reviewable table.
- A single provider's outage or quota exhaustion degrades quality, then degrades to
  retrieval-only, then 503s — three steps, each visible in metrics, none of them touching an
  order.
- Tests get the whole failure taxonomy for free: a scripted `FakeLlm` popping `Completion`s and
  exceptions covers refusal, truncation, rate-limit and unavailability without a network.

**Negative**

- Two SDKs, two credentials, two sets of vendor-specific quirks (streaming frame shapes, tool
  schemas, token accounting) to normalise at the port. The normalisation itself is code that
  can be wrong.
- The router is a new place for a bad decision to hide: a task pointed at the wrong tier fails
  as *quality*, which no alert catches. The nightly eval suite is the only guard.
- Token counting before the call needs a tokenizer per vendor family, and it will be
  approximate for at least one of them.

**Revisit trigger**: a provider offering provisioned throughput that changes the quota
arithmetic in §Context; cost-per-order crossing the tripwire on the cost dashboard; or a third
adapter arriving, at which point "secondary" should become an ordered preference list rather
than a single fallback.
