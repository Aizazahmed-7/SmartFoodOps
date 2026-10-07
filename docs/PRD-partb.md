# SmartFoodOps — Product Requirements Document (Part B: the GenAI assistant plane)

| | |
|---|---|
| **Status** | Approved design baseline |
| **Owner** | QuickServe Platform Engineering |
| **Source of truth** | `SmartFoodOps - Part B.md` (business brief) |
| **Companion docs** | `docs/PRD.md` (Part A), `docs/ARCHITECTURE.md`, `docs/adr/0029`–`0035`, `docs/capacity-plan.md` |
| **Scope** | Part B — GenAI food assistant, semantic discovery, recommendations, explanation engine, restaurant content generation, AI analytics. Part A is amended only where §7 says so. |
| **Numbering** | Continues Part A's shared namespace: **FR-57…FR-104**, **NFR-21…NFR-33**. Part A IDs are never reused or renumbered. |

---

## 1. Overview & problem statement

Part A made ordering work. It did not make deciding *what to order* work, and it left three
kinds of human labour in place:

- **Customers cannot decide.** Discovery is a static browse grid plus a lexical search box. A
  query a person would actually ask — "what should I eat tonight under $10", "something
  light", "spicy, near me" — matches nothing, because the system indexes words, not meaning.
- **Restaurants write everything by hand.** Menu descriptions, promotional copy, engagement
  messages, and any sense of what customers are saying back to them.
- **Delivery problems are silent.** A late order shows a status and a spinner. The platform
  knows *why* — the restaurant has not accepted, the kitchen is at capacity, no rider has
  taken the offer — and tells the customer none of it, which converts a 10-minute delay into
  a support contact.

Part B adds a GenAI plane that answers all three, under one governing constraint inherited
from Part A rather than invented here: **the ordering path never waits on an LLM.**

**Success criteria (product-level)**

1. A customer can ask a vague, natural question and get a grounded answer naming real,
   available, correctly-priced dishes they can order in one tap.
2. Semantic discovery beats lexical search on the vague-query set, and **degrades to lexical
   rather than failing** when the AI plane is down.
3. Every delayed order can explain itself truthfully, from platform facts, with the LLM
   switched off entirely.
4. A restaurant can draft, review and publish menu and promotional copy without writing it
   from scratch — and nothing generated reaches a customer without a human approving it.
5. Order placement, payment, dispatch and tracking availability are **unchanged** by anything
   in this document. The AI plane sheds first and fails alone.

---

## 2. Personas & AI use-cases

| Persona | Description | Primary interfaces |
|---|---|---|
| Customer | Orders food; may be undecided, budget-bound, or waiting on a late order | Customer SPA (chat panel, search, order detail) |
| Restaurant admin | Owns a brand and its branches; writes menus and promos | Partner console (content studio, feedback digest) |
| Ops | Runs the platform; owns the degradation ladder and the spend breaker | Grafana, runbooks |
| System | Consumers, workers, the graph itself | Internal APIs, Kafka, Celery |

### Customer

| UC | Name | Trigger | Main flow | Failure handling |
|---|---|---|---|---|
| UC-18 | Contextual food Q&A | Customer opens the chat panel and asks a question | Classify intent → retrieve (menu chunks ∥ order context ∥ taste profile) → ground → stream answer with real item cards | Provider down → 503 + panel hides, ordering unaffected; empty retrieval → an honest "nothing nearby matches", never an invented dish |
| UC-19 | Vague-preference discovery | "something light", "something spicy" | Query rewrite → hybrid retrieval scoped to deliverable, open branches → ranked dishes | Vector leg down → lexical results only, labelled no differently to the user |
| UC-20 | Budget-bound recommendation | "under $10 tonight" | Price becomes a hard SQL predicate, not a hope expressed in the prompt | Nothing in budget → says so and offers the nearest band |
| UC-21 | Combo / cuisine suggestion | "what goes with this", "Thai near me" | Retrieval within a restaurant or cuisine + co-order signal from item facts | Falls back to popularity within the same restaurant |
| UC-22 | "Why is my order late?" | Customer opens a late order, or asks in chat | Deterministic resolver reads timeline, dispatch row and kitchen load → `ReasonCode` + facts → rendered prose | LLM off → the template answers with the same facts; resolver unsure → `UNKNOWN` and a support hand-off, never a guess |
| UC-23 | Refund / cancellation explanation | Order reaches a cancelled or refunded terminal state | Resolver maps `cancel_reason` + payment state to an explanation | Same as UC-22 |
| UC-24 | Rate an order | Order reaches `DELIVERED` | 1–5 stars + optional free text on the order | Optional; never blocks anything |

### Restaurant admin

| UC | Name | Trigger | Main flow | Failure handling |
|---|---|---|---|---|
| UC-25 | Draft menu descriptions | Admin asks for copy on one item or a whole category | Celery job drafts from item name, tags, category, cuisine → draft rows | Job fails → parked and visible, replayable; never a half-written menu |
| UC-26 | Draft a promotion | Admin describes an offer in a sentence | Draft promo copy + suggested items | As UC-25 |
| UC-27 | Draft engagement copy | Admin wants a message to lapsed or repeat customers | Draft from the restaurant's own aggregate metrics | As UC-25 |
| UC-28 | Feedback digest | Admin opens the feedback tab | Summarise the restaurant's own `order_feedback` rows into themes + representative quotes | Fewer than N rows → shows the raw rows, no summary; never invents a theme |
| UC-29 | Approve & publish | Admin edits and accepts a draft | Draft → menu/promo write through the ordinary Catalog API → re-embed | Rejected drafts are retained for audit; nothing auto-publishes |

### Ops

| UC | Name | Trigger | Main flow | Failure handling |
|---|---|---|---|---|
| UC-30 | Shed the AI plane | Overload, or the spend/quota breaker | Step 2a → retrieval-only; 2b → pause Part B consumers | Automated; ordering untouched at every step |
| UC-31 | Diagnose an AI interaction | A complaint, or an alert | One `trace_id` spans edge → assistant → retrieval → provider; the turn's tokens, model, cache tier and retrieval outcome are on the span | — |

---

## 3. Functional requirements

Priorities: **P0** = required for acceptance; **P1** = completeness; **P2** = stretch.

### 3.1 Knowledge pipeline

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-57 | Menu knowledge ingested from `catalog.changes` | P0 | Consumer group `assistant.knowledge.v1`, 30 s per-restaurant debounce, dedupe mode declared `NATURAL_KEY` (chunk id) per DoD-2; duplicate-delivery and poison-message tests green |
| FR-58 | Chunking + embedding of menu knowledge | P0 | One chunk per item (name + description + tags + category + cuisine) and one per restaurant; `content_hash` skips unchanged text so a replay embeds nothing; `model_version` stamped on every row |
| FR-59 | Index is rebuildable from the log alone | P0 | Truncate `menu_chunks`, start a fresh consumer group, and the index reconstructs from the compacted topic with no backfill script — demonstrated live |
| FR-60 | Volatile facts are never embedded | P0 | Price, availability and open/closed are excluded from chunk text; every item the assistant names is re-resolved through Catalog's cache-bypassing snapshot endpoint before it is rendered |
| FR-61 | Reindex on embedding-model change | P1 | A `model_version` bump drives a Celery rolling reindex; queries filter on the active version so old and new vectors never mix in one result set |

### 3.2 Semantic discovery

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-62 | Hybrid retrieval | P0 | Hard predicates (city, open, branch-only, price band, tags) → lexical leg (existing FTS + `pg_trgm`) ∥ vector leg (HNSW) → reciprocal-rank fusion; ranked candidates carry `item_id`s |
| FR-63 | Geo- and availability-scoped results | P0 | Only deliverable, open branches are candidates; brand template rows are never returned (matching `/v1/search` today) |
| FR-64 | Vague-query handling | P0 | The golden set of vague queries ("something light", "something spicy") returns dishes a human rates relevant; measured by the eval suite, not by assertion |
| FR-65 | `/v1/search` upgraded behind the existing port | P1 | Catalog gains a `HybridSearch` adapter implementing the unchanged five-kwarg `SearchPort`, flag-gated and timeout-bounded, **falling back to `PostgresSearch`**; killing the assistant leaves search working |
| FR-66 | Reranking | P2 | Cheap-tier LLM rerank of top-50 → top-10, off by default, measured against no-rerank on the eval set before it is enabled |

### 3.3 Intelligent food assistant

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-67 | Conversational food Q&A | P0 | `POST /v1/assistant/messages` (auth `customer`\|`restaurant_admin`, `Idempotency-Key`); conversation history persisted; answers cite real dishes with live prices |
| FR-68 | Streaming responses | P0 | Tokens stream over SSE within the existing ticket-auth pattern; time-to-first-token p95 < 1.5 s (NFR-21); the stream path is registered in `stream_prefixes` so it never enters the HTTP latency histogram |
| FR-69 | Stream resume without loss or duplication | P0 | Generation runs decoupled from the connection; chunks carry a `seq`; the stream subscribes **before** it snapshots and drops `seq <= seq_upto`. Kill the connection mid-answer → reconnect resumes exactly (demonstrated live) |
| FR-70 | Grounded output | P0 | Every `item_id` in an answer came from the retrieved candidate set; violations are dropped and counted on `assistant_ungrounded_total`; the metric is 0 across the eval run |
| FR-71 | Prompt-injection resistance | P0 | Restaurant- and customer-authored text is delimited and labelled as data; the eval suite plants injection strings in menu descriptions and feedback and asserts the instruction is not followed |
| FR-72 | Safety refusals | P0 | Allergen-safety and medical questions are refused with a hand-off, never answered from `item_tags`; declared tags may be *surfaced*, never asserted as safety |
| FR-73 | No PII in prompts | P0 | Addresses, phone numbers, emails and full names are redacted before any provider call; user identity in a prompt is an opaque id (NFR-12 extended) |
| FR-74 | Answer caching | P1 | Exact-match then semantic cache, both fenced by `menu_version` and geo bucket; hit ratio visible on `assistant_cache_total{tier,result}` |

### 3.4 Recommendations & order support

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-75 | Personalised recommendations | P0 | Taste profile built offline from item-level order facts + menu views; recommendations for a user with history differ measurably from the cold-start popularity baseline |
| FR-76 | Budget-based recommendations | P0 | A stated budget is a hard predicate; no returned combination exceeds it at live prices |
| FR-77 | Cuisine and combo suggestions | P1 | Combos are drawn from co-order signal within the same restaurant and validated against modifier rules before display |
| FR-78 | Order-assistance explanations | P1 | "What is in this", "how spicy", "what goes with it" answered from menu knowledge, with declared tags only |
| FR-79 | Recommendation acceptance tracking | P0 | `RecommendationShown` / `RecommendationAccepted` carry `item_id`s; acceptance is a join to ordered items within an attribution window, not a client-reported boolean |
| FR-80 | Cold start | P1 | A user with no history gets city- and time-of-day popularity, never an empty response |

### 3.5 Delivery & order explanation engine

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-81 | Order milestone timestamps + events *(amends Part A)* | P0 | `orders` gains `accepted_at`/`preparing_at`/`ready_at`/`picked_up_at`, stamped by the single writer `transition()`; `OrderAccepted`/`OrderPreparing`/`OrderReady`/`OrderPickedUp` publish through the existing outbox. Without these the engine cannot see a delay (§7) |
| FR-82 | Deterministic reason resolver | P0 | A pure function over timeline + dispatch row + kitchen load + the Temporal timer budget returns a closed `ReasonCode` set; unit-tested exhaustively over every branch; the LLM is not consulted to decide *why* |
| FR-83 | Delay explanations | P0 | A deliberately stalled order yields the correct `ReasonCode` and prose that states only resolver-supplied facts |
| FR-84 | ETA reasoning | P1 | Stage-based estimate derived from the timer budget (`accept_timeout_s`, `no_rider_deadline_s`, `pickup_timeout_s`), presented as a range with its basis — never a false precision. Supersedes nothing: OSRM ETAs remain deferred (Part A §7) |
| FR-85 | Restaurant load insight | P1 | Kitchen congestion read from Inventory's `restaurant_load` (`active`/`capacity`) via a new SystemOnly internal endpoint |
| FR-86 | Refund & cancellation explanations | P1 | `cancel_reason` + payment lifecycle map to explanations; the capture-after-delivery design means "refund" explanations describe voids in the common case, truthfully |
| FR-87 | Template-first rendering | P0 | Explanations render from a template cache keyed by `(reason_code, locale, bucket)`; with `llm_api_key=""` the feature still answers, demonstrated live |

### 3.6 Restaurant content generation

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-88 | Menu description drafts | P0 | Celery job per item or category; output lands in `content_drafts`, never in `menu_items` |
| FR-89 | Promotional offer drafts | P1 | Draft copy + suggested items, scoped to the caller's own brand by claim |
| FR-90 | Customer engagement message drafts | P1 | Drafted from the restaurant's own aggregates; contains no customer PII and no fabricated claims or testimonials |
| FR-91 | Order feedback capture *(amends Part A)* | P0 | Customer may rate a `DELIVERED` order 1–5 with optional text; one row per order; nothing to summarise exists without this (§7) |
| FR-92 | Feedback summarisation | P1 | Themes + representative real quotes from the restaurant's **own** rows, claim-scoped so cross-tenant reads are unrepresentable (the FR-55 pattern); below a minimum row count it shows raw rows instead of a summary |
| FR-93 | Human approval before publish | P0 | No generated text reaches a customer without an explicit approve action; publish goes through the ordinary Catalog write path and triggers re-embedding; rejected drafts retained for audit |

### 3.7 AI analytics

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-94 | Assistant interaction facts | P0 | `c1.assistant.events` published **through the outbox** (a KPI, not lossy telemetry); analytics projects one fact row per interaction, absolute values keyed by a natural id so redelivery converges |
| FR-95 | The six required metrics | P0 | AI assistant usage; questions asked/answered; order conversion after AI interaction; recommendation acceptance rate; average AI response time; customer engagement — all served from analytics aggregates and visible in Grafana |
| FR-96 | Item-level order facts *(amends Part A)* | P0 | `order_item_facts` projected from `OrderPlaced.items[]` — `order_facts` carries no item ids, so acceptance rate and taste profiles are unbuildable without it (§7) |
| FR-97 | Conversion attribution | P1 | Interaction → order joined on `(user_id, restaurant_id)` within a bounded window, following the existing `menu_views → order_facts` funnel query |
| FR-98 | Restaurant-facing AI insights | P2 | A restaurant admin sees AI-driven views and conversions for their own brand, claim-scoped like FR-55 |

### 3.8 AI platform & governance (cross-cutting)

| ID | Requirement | Pri | Acceptance criteria |
|---|---|---|---|
| FR-99 | Providers behind a port, two adapters | P0 | `LlmPort` with `AnthropicLlm` + `OpenAiLlm`; refusals/truncations are results, only transport raises; swapping a vendor touches no caller (ADR-0030) |
| FR-100 | Task→model routing with failover | P0 | Closed task vocabulary maps to `ModelSpec`; on `LlmUnavailable`/`LlmRateLimited` the secondary provider is tried once; callers never name a model |
| FR-101 | Budget limits & circuit breaker | P0 | Per-request context cap counted **before** the call; per-user Redis token bucket (TTL set); per-cell spend breaker whose open state degrades to retrieval-only, not to errors |
| FR-102 | AI degradation | P0 | Shed step 2a drives generation → retrieval-only; ordering, payment, dispatch and tracking measurably unaffected under a full AI outage |
| FR-103 | AI observability | P0 | Metrics for response time, TTFT, tokens by direction and model, stream closures by reason, retrieval outcomes, ungrounded rejections, cache tiers; one `trace_id` spans edge → assistant → provider; every alert ships a runbook section |
| FR-104 | Evaluation suite | P0 | `make eval` reports retrieval recall@k / MRR, groundedness, refusal correctness and injection resistance against a golden set; runs nightly in CI, never per-PR |

---

## 4. Non-functional requirements

| ID | Category | Requirement (measurable) |
|---|---|---|
| NFR-21 | Latency — assistant | Time-to-first-token p95 < 1.5 s; complete answer p95 < 6 s; measured on `assistant_time_to_first_token_seconds` / `assistant_response_seconds`, excluding held-connection lifetime |
| NFR-22 | Latency — retrieval | Semantic + hybrid retrieval p99 < 150 ms at the ceiling — the same budget ADR-0019 set for lexical search, because `/v1/search` may now route through it |
| NFR-23 | Availability | Assistant plane 99.5% (deliberately below the ordering path's 99.95%); **order placement availability is unchanged by any AI failure** — proven by a full-outage drill, not by argument |
| NFR-24 | Capacity | At the 2,500 orders/s ceiling: 5% engagement × 3 turns ≈ 375 generations/s ≈ ~50M tokens/min. **Provider quota, not CPU, is the binding constraint** — multi-provider routing, cache tiers and template rendering are load-bearing, not optimisations |
| NFR-25 | Cost governance | Cost-per-order for the AI plane is a first-class dashboard line with a tripwire, derived from a Grafana price table (never hardcoded); per-user and per-cell budgets bound the worst case |
| NFR-26 | Groundedness | 100% of item references in shipped answers resolve to real, currently-available items; 0 prices quoted from the index rather than the live snapshot; `assistant_ungrounded_total` is an alerting signal, not a curiosity |
| NFR-27 | Safety | Allergen/medical questions refused; injected instructions in retrieved content not followed; no PII in any provider payload — each asserted by a nightly eval case, each failure blocking release |
| NFR-28 | Freshness | A committed menu change is visible to retrieval within 60 s p99 (30 s debounce + embed + upsert); staleness beyond that pages |
| NFR-29 | Degradation | Ladder step 2a is automated and reversible; every AI feature has a defined non-AI answer (lexical search, popularity recommendations, templated explanations); no feature's failure mode is a blank screen |
| NFR-30 | Observability | Retrieval pipeline failures, streaming interruptions, LLM latency and provider failures are each diagnosable from one `trace_id`; graph nodes are spans; `stream_prefixes` keeps stream lifetimes out of latency percentiles |
| NFR-31 | Evaluation | The golden set is versioned in-repo and grows with every incident; a regression in recall@k, groundedness, refusal correctness or injection resistance fails the nightly run |
| NFR-32 | Privacy & retention | Conversations retained 90 days then purged; a user deletion request removes conversation rows and taste profile; no PII in `assistant.events` payloads (Part A's anti-pattern #25 applies unchanged) |
| NFR-33 | Developer experience | `make up-ai` runs the AI plane beside the core stack on a 16 GB laptop; **the unit suite requires no API key and no network** (`llm_api_key=""` disarms); `make cov` stays at 100% with `--cov=ai_assistant` |

---

## 5. Milestones

Indicative durations (proposed, single team): B0 ≈ 1 week, B1–B2 ≈ 2 weeks, B3 ≈ 2 weeks,
B4–B6 ≈ 3 weeks, B7 ≈ 1 week. Every milestone ends with an adversarial review scaled to risk,
a walkthrough in `docs/reviews/`, `make lint` + `make cov` green at 100%, and live verification
against the running stack — CI never runs compose, so live proof is the standard.

| Phase | Scope | Entry criteria | Exit criteria |
|---|---|---|---|
| **B0 — Foundations** | ADRs 0029–0031, this PRD, service skeleton, all ports + fakes, `ModelRouter`, budget breaker, metrics, compose/Makefile/CI wiring, eval harness skeleton | Part A on `main`, green | `make up-ai` green; `/healthz`+`/readyz`+`/metrics`; an internal echo route streams tokens live from **both** providers; 100% coverage with the fake LLM and no key present |
| **B1 — Knowledge pipeline** | Chunking, `EmbeddingPort`, pgvector schema + per-city HNSW, `assistant.knowledge.v1`, Celery reindex | B0 exit **+ the pgvector image route decided in ADR-0032**: the obvious tag swap is a glibc downgrade the existing volume rejects (found live in B0, `docs/local-dev.md` §11) | Live menu edit visible in `menu_chunks` inside the debounce window; **truncate the table, replay the topic, the index rebuilds identically**; duplicate-delivery + poison tests green |
| **B2 — Semantic discovery** | Hybrid retriever + RRF, internal retrieval API, Catalog `HybridSearch` adapter with lexical fallback, FE search | B1 exit | Vague-query golden set passes; retrieval p99 < 150 ms; **kill the assistant → `/v1/search` still answers** |
| **B3 — Food Q&A + streaming** | LangGraph turn, SSE token relay, conversation store, grounding validator, guardrails, FE chat panel, first `assistant.events` | B2 exit; ADRs 0034–0035 accepted | Streamed grounded answer end-to-end; **connection killed mid-answer resumes with no gap or duplicate**; planted injection not followed; provider down → 503 and the panel degrades |
| **B4 — Recommendations** | `order_item_facts`, taste profiles, budget/cuisine/combo recommenders, acceptance tracking, FE surfaces | B3 exit | Every recommendation exists, is available, and is priced live; acceptance rate measurable end to end |
| **B5 — Explanation engine** | Order milestone timestamps + events, reason resolver, stage-based ETA, template cache + renderer, FE on order detail | B4 exit | A stalled order yields the correct `ReasonCode` and a truthful explanation; **`llm_api_key=""` → templates still answer** |
| **B6 — Restaurant content studio** | Feedback capture, draft generation, summarisation, approve→publish, Celery queues + DLQ, partner console tab | B5 exit | Draft → edit → publish updates the menu **and** re-embeds; a parked job is visible and replayable; a summary cites only real rows |
| **B7 — Analytics, evals, capacity** | Analytics tables + endpoints, conversion attribution, Grafana `assistant.json`, alerts + runbooks, nightly evals, capacity-plan §AI, cost dashboard | B6 exit | All six required metrics live; every alert has a runbook; `make eval` reports the full rubric; the AI-outage drill shows ordering unaffected |

---

## 6. Traceability matrix

Every FR maps to implementing component(s), the topic or graph stage that carries it, and its
delivering milestone. Reviewers: this is the completeness check against the Part B brief.

| FR | Summary | Component(s) | Topic / stage | Phase |
|---|---|---|---|---|
| FR-57 | Menu knowledge ingestion | ai-assistant consumers | `c1.catalog.changes` (`assistant.knowledge.v1`) | B1 |
| FR-58 | Chunking + embedding | ai-assistant `domain/knowledge`, `EmbeddingPort` | — | B1 |
| FR-59 | Rebuildable index | ai-assistant consumers | `c1.catalog.changes` (compacted) | B1 |
| FR-60 | No volatile facts embedded | ai-assistant, Catalog snapshot endpoint | — | B1 |
| FR-61 | Model-version reindex | Celery `assistant.reindex` | — | B1 |
| FR-62 | Hybrid retrieval | ai-assistant `retrieval/`, `VectorStore` | — | B2 |
| FR-63 | Geo/availability scoping | ai-assistant `retrieval/` | — | B2 |
| FR-64 | Vague-query handling | ai-assistant `retrieval/`, eval suite | — | B2 |
| FR-65 | `/v1/search` upgrade | Catalog `HybridSearch` adapter → ai-assistant | — | B2 |
| FR-66 | Reranking | ai-assistant, cheap-tier model | `RERANK` task | B2 (P2) |
| FR-67 | Conversational Q&A | ai-assistant `api/`, `domain/graph` | `GENERATE` task | B3 |
| FR-68 | Streaming responses | ai-assistant, `smartfood-realtime`, nginx `/sse/assistant` | `sfo:assist:{message_id}` | B3 |
| FR-69 | Stream resume | `smartfood-realtime.stream_relay`, `message_chunks` | `sfo:assist:{message_id}` | B3 |
| FR-70 | Grounded output | ai-assistant `domain/grounding` | — | B3 |
| FR-71 | Injection resistance | ai-assistant prompt assembly, eval suite | — | B3 |
| FR-72 | Safety refusals | ai-assistant `domain/policy` | — | B3 |
| FR-73 | No PII in prompts | ai-assistant `domain/redaction` | — | B3 |
| FR-74 | Answer caching | ai-assistant, Redis `assistant:*`, `answer_cache` | — | B3 |
| FR-75 | Personalised recommendations | ai-assistant, `taste_profiles` | `c1.orders.events` (`assistant.features.v1`) | B4 |
| FR-76 | Budget recommendations | ai-assistant `retrieval/` | — | B4 |
| FR-77 | Cuisine / combo | ai-assistant, `order_item_facts` | — | B4 |
| FR-78 | Order-assistance answers | ai-assistant `domain/graph` | — | B4 |
| FR-79 | Acceptance tracking | ai-assistant, Analytics | `c1.assistant.events` | B4 |
| FR-80 | Cold start | ai-assistant, Analytics popularity | — | B4 |
| FR-81 | Milestone timestamps + events | **Order**, `smartfood-kafka` | `c1.orders.events` | B5 |
| FR-82 | Reason resolver | ai-assistant `domain/explain` (pure) | — | B5 |
| FR-83 | Delay explanations | ai-assistant, Order, Dispatch | `EXPLAIN` task | B5 |
| FR-84 | ETA reasoning | ai-assistant `domain/explain` | — | B5 |
| FR-85 | Restaurant load insight | **Inventory** internal endpoint, ai-assistant | — | B5 |
| FR-86 | Refund / cancellation explanations | ai-assistant, Order, Payment | — | B5 |
| FR-87 | Template-first rendering | ai-assistant template cache | — | B5 |
| FR-88 | Menu description drafts | ai-assistant Celery, `content_drafts` | `assistant.content` queue | B6 |
| FR-89 | Promotion drafts | ai-assistant Celery | `assistant.content` queue | B6 |
| FR-90 | Engagement drafts | ai-assistant Celery, Analytics | `assistant.content` queue | B6 |
| FR-91 | Feedback capture | **Order** (`order_feedback`), FE | — | B6 |
| FR-92 | Feedback summarisation | ai-assistant Celery | `SUMMARIZE` task | B6 |
| FR-93 | Human approval before publish | Partner console, Catalog write path | `c1.catalog.changes` (re-embed) | B6 |
| FR-94 | Interaction facts | ai-assistant outbox, Analytics | `c1.assistant.events` | B3 → B7 |
| FR-95 | The six metrics | Analytics, Grafana | — | B7 |
| FR-96 | Item-level order facts | **Analytics** projector | `c1.orders.events` | B4 |
| FR-97 | Conversion attribution | Analytics | — | B7 |
| FR-98 | Restaurant AI insights | Analytics, partner console | — | B7 (P2) |
| FR-99 | Provider port | ai-assistant `domain/ports`, adapters | — | B0 |
| FR-100 | Task routing + failover | ai-assistant `ModelRouter` | — | B0 |
| FR-101 | Budgets + breaker | ai-assistant, Redis `assistant:*` | — | B0 |
| FR-102 | AI degradation | ai-assistant, **edge-bff**, ADR-0014 ladder | — | B0 → B7 |
| FR-103 | AI observability | `smartfood-otel`, ai-assistant `metrics.py`, Grafana | — | B0 → B7 |
| FR-104 | Evaluation suite | `tools/eval`, CI nightly | — | B0 → B7 |

---

## 7. Part A amendments — the honest audit

Part A's success criterion #5 promised Part B would land "as ordinary Kafka consumer groups
with **zero Part A changes**". That promise **holds** for embeddings (FR-57–FR-61), streaming
transport (FR-68), telemetry (FR-94) and shedding (FR-102) — each attaches to a hook that
already exists. It **does not hold** in four places, listed here rather than quietly absorbed.

| # | Amendment | Why the promise could not hold | Where | Milestone |
|---|---|---|---|---|
| 1 | Order milestone timestamps + 4 new events (FR-81) | Part A deliberately publishes *"major states only (no per-transition spam)"* and overwrites `orders.updated_at` on every move. An explanation engine cannot be truthful about a delay it cannot see. The transitions already route through `transition()`, which stages events in the same transaction — only the `event=` argument and four nullable columns are missing. Part A's own FR-55 already wants prep time | Order, `smartfood-kafka` | B5 |
| 2 | `order_item_facts` projection (FR-96) | `order_facts` carries no item ids, so recommendation acceptance and taste profiles are unbuildable. `OrderPlaced` already carries `items[]` — this is a new consumer, no producer change | Analytics | B4 |
| 3 | `restaurant_load` read endpoint (FR-85) | `active`/`capacity` is the only kitchen-congestion signal in the system and is not published anywhere | Inventory | B5 |
| 4 | `order_feedback` capture (FR-91) | The brief asks for feedback summaries; Part A captures **no customer feedback of any kind**. Summarising proxies (cancel reasons, delivery times) would be dishonest framing | Order, FE | B6 |

Plus these mechanical additions, which change no Part A behaviour: `stream_relay()` in
`smartfood-realtime`; `Topic.ASSISTANT_EVENTS` and the new `EventType` members; an edge rule
and `AI` rate-limit class; an `/sse/assistant` nginx lane; the ADR-0014 ladder split;
`SERVICE_PACKAGES` in the layer-contracts scan; and the usual compose/Makefile/pyright/cov
wiring.

Nothing in this document touches money, the saga, the guarded status writer, or the dispatch
lock.

---

## 8. Out of scope / deferred

| Item | Status | Hook that keeps it cheap later |
|---|---|---|
| **A dedicated retrieval service** | Deferred | Retrieval sits behind `VectorStore` + an internal HTTP API from day 1; the split trigger (index QPS or corpus size) is named in ADR-0029 §5 |
| **OpenSearch migration** | Deferred | ADR-0019's triggers still govern; `SearchPort` is unchanged, so the swap stays an adapter |
| **Cross-encoder reranking** | Deferred | FR-66's cheap-tier rerank is the measurement that would justify it |
| **Fine-tuning / custom models** | Out of scope | `ModelRouter` maps a task to a model id; a tuned model is a new row |
| **Multilingual retrieval** | Deferred | Catalog's FTS config is `'simple'` (no stemming) and embeddings are multilingual-capable; the blocker is the eval set, not the index |
| **Voice input, dish-photo understanding** | Out of scope | No image or audio data exists in the Part A schema (`docs/ARCHITECTURE.md` §10 plans image URLs only) |
| **AI placing orders on a customer's behalf** | Out of scope | If ever wanted, it calls `POST /v1/orders` and the saga owns it — the assistant never orchestrates a money path (ADR-0031) |
| **Structured allergen / nutrition data** | Deferred | `item_tags` is the only declared attribute mechanism today; FR-72 refuses safety claims precisely because the data to make them does not exist |
| **Bedrock / Groq adapters** | Deferred | `LlmPort` + a `ModelRouter` row (ADR-0030) |
