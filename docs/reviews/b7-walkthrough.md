# B7 — Measuring the assistant walkthrough (2026-10-05)

B3 through B6 built an assistant and gave it work to do. B7 asks the only
question that was still open: **is any of it working, and what is it
costing?**

That makes this milestone different in kind from the ones before it. Nothing
here is a feature a customer can see. Every slice is about whether a number
on a screen can be believed — and the recurring failure mode throughout was
not a crash but a **confident wrong number**: a metric that keeps rendering
while quietly describing something other than its label. Four of the five
bugs found during the build were of exactly that shape, and none of them
would have shown up as an error.

## What landed

| Slice | Piece | Where |
|---|---|---|
| 7.1 | Interaction facts (FR-94) | `assistant_facts`, analytics migration 0007, `AssistantFactsProjector` |
| 7.2 | The six metrics (FR-95) | `GET /v1/internal/analytics/ai`, four aggregate reads |
| 7.3 | Conversion attribution (FR-97) | `assistant_conversion`, counted from both ends |
| 7.4 | Restaurant AI insights (FR-98) | `restaurant_brands`, migration 0008, `GET /v1/restaurant/analytics/ai` |
| 7.5 | Dashboard, alerts, runbooks | `assistant.json`, 9 alerts, 10 runbook sections |
| 7.6 | Cost governance, capacity, evals | `llm-costs.yml`, capacity-plan §7, `nightly-eval.yml` |
| 7.7 | The outage drill and this record | below |

## The decisions worth remembering

**Facts, not counters.** `assistant_facts` is keyed by `message_id` and
carries absolute values. The outbox is at-least-once and its poller re-sends
anything it published but did not mark, so a counter would inflate every KPI
each time a poller crashed mid-flight. This is the same rule `order_facts`
follows, applied to a table whose whole purpose is to be summed.

**`DO UPDATE`, not `DO NOTHING`** — the opposite call from `insert_item_facts`
one file over. An item fact is written once from `OrderPlaced` and never
changes, so a redelivery has nothing to say. An interaction fact *can*
legitimately be restated: a turn that streamed and then failed at the last
token settles twice, and the second fact is the true one.

**Response time is averaged three ways because one way is a lie.** A cache
hit returns in milliseconds and a generated answer in seconds. The blended
mean describes neither experience and drifts with the hit rate rather than
with the system getting faster — 2425 ms blended against 4837 ms generated,
on the live data, on day one.

**"Answered" means exactly the `answered` outcome.** A refusal and a
no-match are honest replies that answered nothing. Folding them in would
make the rate *rise* as the assistant got less useful, which is the
direction a metric must never move for free.

**Acceptance is scored per turn, not per item.** One answer naming five
dishes is one recommendation a customer acts on or does not. Per-item
scoring caps a perfect answer at 20%, so the rate would fall every time the
answers got more helpful.

**Conversion is counted from both ends, and the two numbers do not match on
purpose.** `converted_turns` is turn-side: of the turns that pointed a
customer somewhere, how many were followed by an order there.
`attributed_orders` is order-side and distinct — three questions about one
restaurant followed by one dinner are three turns that worked and **one**
order. Revenue hangs off the order count, or it is multiplied by however
many questions the customer happened to ask first.

**The caveat ships in the payload.** `"basis": "correlation within the
window; not a controlled measurement"` is a response field, not a docstring,
because the number travels further than the code does.

**FR-98 is bridged by a mapping, not a column.** A citation names a BRANCH —
that is what the index carries — while a restaurant admin's claim is
normally the BRAND. Denormalising `brand_ids` onto the event would have
needed a cross-service producer change and still left three `recommend.py`
paths emitting blanks. `catalog.changes` is compacted and carries every
branch, so a mapping fed from it is **complete** without a backfill and
**current** through a repoint, which a frozen array would not be.

**Prices live in one file that both the dashboard and the alert read.**
NFR-25 asks for "a Grafana price table", but the tripwire is a Prometheus
alert and an alert cannot read a Grafana variable. One joinable table is the
only arrangement with a single source of truth.

## What it costs

At the ceiling (capacity-plan §7): 375 turns/s, ~833k tokens/s, **~333
tokens per order**. The binding constraint is **provider quota, not CPU**,
and it is a vendor's number rather than ours — which is why multi-provider
routing, the cache tiers and the templated paths are load-bearing rather
than optimisations.

The budget is **$0.004 per order**, ~60× the modelled flash-class cost and
~2.4× premium-class. The asymmetry is deliberate: it has to survive a
deliberate model upgrade without paging anyone, while still catching a loop
or a cache gone cold. A tripwire that fires on a planned decision gets
silenced, and then it is not a tripwire.

The live price table ships with **placeholder numbers**, clearly marked.
`AssistantSpendUnpriced` exists because that design has exactly one failure
mode: a model with no price row drops out of the join, and the cost line
goes *confidently low* with no gap in it.

## What is proven, and how

Measured on the running stack, not argued:

| Claim | Evidence |
|---|---|
| Redelivery converges | Reset the consumer group: 98 events re-consumed, all 24 rows re-written, table still 24 rows |
| The cache split is real | 2425 ms blended vs **4837 ms generated** on live data |
| Acceptance/conversion SQL works on Postgres | Rolled-back probes: 3 converted turns → **1** attributed order, 2500 cents settled |
| The 24h window is one-sided and bounded | Three turns differing only in the wait: 23h after accepted, 25h after rejected, 1h before rejected |
| Brand scoping is real, not a pass-through | Brand A reads 14 turns, brand B reads its own 12, an unknown brand reads 0 |
| A two-branch brand aggregates both | Rolled-back probe: 2 turns from 2 branches under one claim |
| Dashboard SQL actually runs | All 7 panels executed as `grafana_ro`; a query through Grafana's own proxy returned 24 |
| The price join computes real money | 279 prompt × $0.10/1M + 73 completion × $0.40/1M = $0.0000571, exact |
| The unpriced detector works both ways | 2 series before prices loaded, 0 after |

### The AI-outage drill (NFR-23)

NFR-23 says order placement availability is unchanged by any AI failure, and
that it is proven **by a full-outage drill, not by argument**. Here is the
drill.

First, where the two planes actually touch. Only two places reference the
assistant from Part A: the edge-bff proxy route (customer-facing, so an
outage there is an assistant outage by definition) and **catalog's
`/v1/search`**, which routes through the assistant's retriever when
`hybrid_search=on` with `PostgresSearch` as the fallback. Order placement
itself has no call into the AI plane at all — so the interesting question is
not placement, it is whether search drags placement down with it.

Three phases, all against the live stack:

| Phase | Order to SETTLED | Search (city-scoped) | Path | Assistant endpoint |
|---|---|---|---|---|
| AI plane up | **60 s** | 440–740 ms, 12 results | `hybrid` | 200 |
| AI plane hard down | **49 s** | 15–28 ms, 9 results | `lexical` | 503 in 8 ms |
| Assistant black-holed | — | **2.019 s**, 9 results | `lexical` | — |

**Placement is unaffected.** A full order walked PLACED → CONFIRMED →
ACCEPTED → PREPARING → READY → PICKED_UP → DELIVERED → SETTLED with the
whole AI plane stopped, in 49 s against a 60 s baseline.

**Search degrades in quality, not availability** — 9 lexical results instead
of 12 hybrid ones, which is exactly what NFR-29 asks for. The assistant's own
endpoint answers a structured `503 DEPENDENCY_UNAVAILABLE` in 8 ms rather
than hanging: a screen the frontend can render, not a blank one.

**The two failure modes are not equally cheap, and that is the finding.** A
*stopped* assistant refuses connections instantly, so the fallback costs
nothing — search actually got faster. A *hung* one (simulated by pausing the
container: TCP accepts, nothing answers) burns the full configured timeout on
**every city-scoped search**: 2.019 s, measured, three times running. That is
a 13× breach of ADR-0019's 150 ms p99 search budget, sustained for as long as
the assistant stays hung, with no circuit breaker to stop paying it. Search
keeps answering, so nothing alerts and nothing fails over — it just gets
slow and stays slow. See **Known limits**.

A note on method: the first run of this drill measured nothing. Search was
called without a `city`, and `HybridSearch` short-circuits to lexical when
the scope is absent — so all three phases were timing the fallback path and
reporting a reassuring flat line. The `X-Search-Path` response header is
what exposed it. A drill that cannot tell you which leg it exercised is a
formality.

## The adversarial review

Four reviewers over the B7 diff: analytics KPI correctness, FR-98 tenancy,
observability config, and the assistant/eval changes. They found more than
the build did, and two of the findings contradicted claims this milestone
had already made in writing.

### Fixed

**The degradation switch never worked (CRITICAL).** 7.6 closed a debt by
making `generation=off` reach the content worker, and said so. The reviewer
found it had never reached the **customer chat path** either.
`AssistantService.prepare` was the only reader of the flag, and `prepare` is
called from exactly two places — `/v1/internal/assistant/echo` and
`/v1/internal/assistant/stream`, both internal diagnostics. So an operator
could throw the switch during a spend incident, smoke-test the echo
endpoint, get a 503, record the ladder step as applied, and have every
customer question, every explanation polish and every content draft keep
generating at full rate. **A switch that reports success without acting is
worse than no switch.**

The fix moves enforcement to the `ModelRouter`, which is the one place every
generation converges — chat, polish, drafts, summaries, and anything added
later. A new call site cannot forget to ask, because it cannot reach a
provider without coming through it. `PlaneShed` moved to `ports` alongside
the other domain exceptions to break the resulting import cycle.

Proven live, which is the only way this claim should ever be made again:

```
generation=off → "PlaneShed: generation disarmed (ladder step 2a)",
                 assistant_tokens_total: no series at all
generation=on  → same question, 856 prompt + 100 completion tokens
```

Note what still happened with the switch off: the interaction fact was
published anyway. A shed turn is a fact about a customer who asked and did
not get an answer, and it belongs in the KPI.

**The nightly swallowed its own exit code (CRITICAL).** `make eval | tee`
under Actions' default `bash -e {0}` takes `tee`'s status — always 0. Both
exit 1 (a regression) and exit 2 (strict: nothing measured) would have been
discarded and the nightly would have been green, which is exactly what
strict mode was added to prevent. `shell: bash` is what adds `-o pipefail`.
The `if: failure()` log step would never have fired either.

**Turn-side conversion was scoped on one end (HIGH), and wrong in both
directions.** `restaurant_assistant_conversion` matched the order against
any *cited* branch rather than against the claimant. It over-counted: a turn
naming me and a competitor, followed by dinner at the competitor, became my
100% conversion — other tenants' orders moving my rate, and a reading
channel onto a co-mentioned competitor's traffic. It under-counted too: a
brand whose assistant named one branch lost every customer who ordered from
a sibling branch. Owner-level scoping fixes both.

**My own test should have caught it and did not.** It asserted only
`attributed_orders`, while `converted_turns` sat at 1 and `conversion_rate`
at 1.0 in the same fixture — a dashboard reading "100% conversion, zero
orders", self-contradictory on its face. The test now asserts all three, and
reverting the predicate fails exactly the two tests that describe the two
directions.

**Anonymous turns became one returning regular (HIGH).** The acceptance and
conversion joins excluded an empty `user_id`; the three *counting* queries
did not. Two strangers collapsed into one synthetic person who, sharing a
`user_id` with every other anonymous turn, also looked like a returning
customer — `returning_rate` wrong in both numerator and denominator. A named
`_people()` helper now keeps the three from drifting apart again.

Underneath it: `str(payload.get("user_id", ""))` only defaults when the key
is **absent**, so an explicit null produced the literal string `"None"`,
which passes every `user_id != ""` guard downstream.

**The skip contract covered one field (HIGH).** `assistant_values` promises
in its docstring that an unreadable payload is skipped rather than parking
the batch — "a KPI projection that stops on one bad row stops counting
everything". Only `message_id` was guarded. A missing envelope timestamp, a
non-numeric duration or a non-iterable id list raised straight out of
`handle_batch`, taking the good facts in the same batch with it and halting
the group. Every coercion is guarded now, and the test covers four shapes
rather than one.

**An empty answer scored well (HIGH).** The eval's `ask()` returned
`Answer(text="")` when a stream ended with no frames — and an empty answer
passes the injection rubric trivially, since nothing leaked. A nightly in
which the assistant said literally zero words would have reported
`injection 4/4 pass`. That is the outcome `report.py` was written to make
impossible, arriving through a *populated* rubric scoring a *vacuous*
answer. `NoAnswer` now skips the rubric, which strict mode turns into exit 2.

**The staleness page could not fire in its own outage (HIGH).**
`AssistantKnowledgeStale` read the freshness histogram, which is observed
**only when a restaurant is successfully indexed**. So with the embedding
provider hard-down, every pass raises, nothing is ever observed, every
bucket's rate is zero, `histogram_quantile` returns NaN, and `NaN > 60` is
false — the page NFR-28 explicitly demands stayed inactive in exactly the
outage it exists for, while the dashboard showed a flat gap that reads as
"no menu changes". It now reads a new `assistant_knowledge_backlog_seconds`
gauge, set by the drain on every pass **before** it tries to drain, so a
pass that fails on every row still reports the backlog it could not clear.
The failure modes are opposite: a histogram of successes goes quiet when
things break, a backlog gauge rises.

**The cache-hit alert measured the wrong thing (HIGH).** The two tiers are
sequential — `exact_cache → retrieve → semantic_cache → generate` — and both
increment the same counter, so the semantic tier is only consulted after an
exact *miss*. Summing both counted a turn once or twice depending on whether
the first tier hit, and the bias ran the wrong way: **a healthier semantic
tier pushes the computed rate down**. 10 exact hits plus 20 semantic hits
over 100 turns is a real 30% rate, computed as 15.8% — a page for beating
the floor. The denominator is now the exact tier's lookups, which is one per
turn.

Four more alerts could not fire when they mattered, all now corrected: the
unpriced-tokens alert had `for: 15m` against a 10m rate window, so a worker
job on a 15-minute schedule would reset its pending state every cycle and
never fire — guarding the cost design's one blind spot with a condition that
could not hold; the failover alert was an absolute rate where the condition
is a ratio, so a cell at 2 turns/min could lose its primary provider
entirely without reaching it; both cost tripwires sat behind an order floor
of 3 orders/min, filtering out exactly the low-volume incidents where
cost-per-order is highest; and the shedding alert fired on a single routine
per-user refusal, burying the cell-wide breaker it was meant to surface.

**Nothing alerted on the worker vanishing.** The scrape job added in 7.5 is
`dns_sd`, so a dead container makes `up` *absent* rather than zero and
`ServiceDown` matches nothing — the trap this very file already documents
for `order-worker`. `AssistantWorkerTargetAbsent` closes it.

**The dashboard reintroduced a bug the API had just fixed.** Panel 7 counted
`DISTINCT user_id` including the empty anonymous bucket, while `_people()`
excludes it — the one place the dashboard and the API answered the same
question differently. Two cost panels also rendered +Inf on a window with no
placements; both are bounded now.

Also fixed: a shed no longer permanently burns an explanation-polish key
(it is a reversible decision, not a model failure); the failure-log step
names the compose profile that owns the service; and `ai_metrics`' docstring
no longer claims conversion is reported as null.

## Known limits

Carried deliberately, with the reasoning, rather than quietly:

**A hung assistant costs every city-scoped search 2 s.** Measured in the
drill. Bounded and still answering, but a 13× breach of ADR-0019's 150 ms
p99 for as long as it lasts, and nothing alerts because search never fails.
A circuit breaker on the hybrid leg is the fix; it is a Part A change to
`HybridSearch` and did not belong in a measurement milestone.

**NFR-22 has no instrument.** Retrieval p99 < 150 ms is a stated SLO with no
histogram behind it, so it cannot be alerted on. Needs a producer change in
the retriever.

**Conversion and acceptance rates are right-censored.** The denominator
counts turns whose 24 h window has not elapsed, so the same traffic reports
a lower rate at `days=1` than at `days=30` — an owner comparing the two sees
a "collapse" that is pure windowing. The fix is to bound the denominator at
`now - 24h` or publish the maturity cutoff beside the rate; both change what
the number means, which is a decision rather than a patch.

**`record_brand` is last-arrival-wins.** It stores an `updated_at` nobody
reads, so an out-of-order replay writes the older brand. Catalog has no
repoint path today, so a replay rewrites the same value — but the guard
belongs in the WHERE clause per this repo's own rule, and should land before
a repoint feature does. Related: a branch whose `brand_id` becomes null is
never *un*mapped, so a de-branded restaurant would stay visible to its old
owner. Both are latent on a path that does not exist yet.

**A park can itself fail.** `_run_job`'s catch-all opens a fresh engine to
park the draft, so the database blip its docstring names as the motivating
case would also break the park — leaving the row `queued`, which no human
action can reach. The same catch-all swallows `LookupError`, which
`content.py` says should fail loudly, and reports the task as succeeded.

**Embeddings and the rolling reindex are not shed.** Up to 10,000 embedding
calls per reindex invocation, outside ladder step 2a. "Embeddings are not
generation" is a defensible reading and the compose comment claims the
opposite; it needs a recorded decision, not silence.

**The nightly's `MODEL_GENERATE`/`MODEL_CHEAP` reach nothing** (compose
plumbs them only to the worker, which `make up-ai` does not start), and its
`/readyz` wait does not wait for the first embed pass — so the retrieval
rubric can skip on a race. Both make the nightly less useful than it looks;
neither makes it lie, now that strict mode is armed.

**Pre-existing, found in passing:** `acceptance_rate = 1 - rejected/confirmed`
in `ops_metrics`/`restaurant_metrics` uses disjoint populations as a ratio —
a rejected order never gets a `confirmed_at` — so 1 confirmed and 3 rejected
yields `-2.0`. It predates B7 and sits in the Part A surface; flagged, not
touched.

## What B7 leaves behind

The assistant can now be argued about with numbers instead of impressions,
and the numbers have an audit trail: facts keyed by a natural id, rates that
answer null rather than zero, a cost line whose prices live in one file, and
a tripwire that survives a deliberate model upgrade.

The thing worth carrying forward is the failure mode this milestone kept
meeting. Nothing here ever crashed. A rule file that was never mounted, a
recording rule that stored nothing while reporting healthy, a drill that
measured the fallback path in all three phases, a kill switch that answered
503 from the two endpoints nobody uses — every one of them looked fine and
was wrong, and every one was caught by running it rather than by reading it.
