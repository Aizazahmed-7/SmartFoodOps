# Alert runbooks

One section per alert rule (`deploy/compose/observability/alerts.yml`). Every
section answers three questions in order: what does this MEAN, what do I CHECK
first, what FIXES it. The universal scan order behind all of them:
**Readiness panel → 5xx panel → domain panels**, then Jaeger for the failing
hop, then logs by `trace_id`. Dashboard: http://localhost:3000/d/smartfoodops-slo

## ServiceDown

**Means**: Prometheus could not scrape the target — the process is dead,
crash-looping, or unreachable. This is the only alert that does not depend on
the sick service reporting its own sickness.
**Check**: `docker compose ps <svc>` (crash-loop shows recent restart);
`docker compose logs --tail 30 <svc>` for the exit reason. Common local causes:
stale Docker DNS after a daemon restart (bounce the app tier), a migration
mismatch after a branch switch (`alembic can't locate revision` → `make nuke`),
a workspace member missing `pyproject.toml`.
**Fix**: address the exit reason; `docker compose --profile core --profile apps
restart <svc>`. Verify: the `up` line returns to 1 within one scrape (15s).

## HttpErrorRateHigh

**Means**: >5% of a service's responses are 5xx over 10m. It fires on the
VICTIMS — the culprit is usually below them (a 503 on order + edge usually
means a dependency of order).
**Check**: which jobs fire together (the blast-radius boundary points at the
cause); one `error=true` trace in Jaeger — the failing hop is where child
spans stop appearing (diagnosis by absence).
**Fix**: per the failing hop. Verify: ratio decays out of the 10m window.

## PlacementP95Breach

**Means**: fresh placements (`outcome="placed"` only) breach NFR-2's 3s p95.
Replays/refusals are excluded by design — this is real customer wait.
**Check**: placement-outcomes panel for a `pending` shift (worker starvation —
the 2s await losing to schedule-to-start); one slow placement trace — the fat
span or the gap inside `StartUpdateWithStartWorkflow` names the layer
(worker queue vs identity vs catalog vs DB).
**Fix**: worker starvation → scale order-worker; dependency slowness → that
service's runbook. Verify: p95 line bends back under 3s.

## OutboxPublishLagHigh

**Means**: events are flowing but STALE (staged→published p99 > 5s, NFR-6).
Every consumer is reacting to the past.
**Check**: backlog series next to it — lag high + backlog falling = burst
being digested (usually no action); backlog rising = poller/Kafka trouble.
Poller logs in the OWNING service (`outbox drain failed — will retry`).
**Fix**: Kafka health; poller crash-loop; DB contention on the outbox table.

## OutboxBacklogGrowing

**Means**: >100 rows staged-but-unpublished for 10m. The gauge is set at the
START of every pass (success or not), so this fires even when Kafka is fully
down and the lag histogram has gone silent.
**Check**: is ANYTHING publishing (`rate(outbox_published_total[5m])`)?
Zero = the poller or Kafka is down, not slow. Kafka container, then poller logs.
**Fix**: restore Kafka / restart the owning service. The backlog self-drains —
rows are never lost, only late. Verify: gauge falls; consumers catch up.

## EventsParkedOnDLQ

**Means**: a consumer exhausted five retries (or hit undecodable bytes) and
parked the ORIGINAL event on `<topic>.dlq` (ADR-0021). Facts are waiting for
a human; nothing is lost, something is stuck.
**Check**: which `group` label fired; the consumer's logs for `event parked to
DLQ` (carries error type + source offset); the DLQ topic in the Kafka console
(:8085) — `dlq.*` headers hold the forensics.
**Fix**: fix the handler/dependency, then `make dlq-replay
TOPIC=<source-topic>.dlq` to re-feed the parked events. Undecodable bytes
(SerdeError, attempts=0) can never replay — investigate the producer instead.

## SystemTimeoutCancels

**Means**: the saga's forward deadline (default 300s) expired — reserve/
authorize/confirm could not complete while a customer's order was live, and it
was unwound to CANCELLED `system_timeout`. Customers were notified and never
charged; the alert is about the DEPENDENCY that stayed down.
**Check**: which downstream was unreachable — the workflow history in Temporal
UI (:8233, `ord::{order_id}`) shows the retrying activity and its error;
usually pairs with a ServiceDown/5xx alert naming the culprit.
**Fix**: restore the dependency. Orders cancelled this way are correctly
terminal — customers re-order; nothing needs replaying.

## ConsumerLagGrowing

**Means**: a consumer group is >100 events behind its topic for 10m — it is
processing slower than producers produce (kafka-exporter reads the broker's
own books, so this is real lag, not a proxy).
**Check**: the consumers panel — is the group's `handled` rate flat while
outbox `published` climbs? Any `retried`/`dlq` for the group (a struggling
dependency slows every event)? cAdvisor CPU for the consuming service.
**Fix**: dependency slowness → that runbook; genuine throughput → scale the
consumer — BUT replicas beyond the topic's partition count sit idle
(partitions are the parallelism ceiling; dev topics have 1). Verify: lag
trends to zero; it self-drains, nothing is lost.

## CanarySilent

**Means**: the synthetic customer (canary, obs profile) has not completed a
full place→CONFIRMED→cancel loop in 5 minutes. Whatever the per-service
panels say, the PRODUCT is not working end to end.
**Check**: the canary's own logs name the failing step (`docker compose logs
canary`); then treat it as a customer report — the placement runbooks apply.
Usually pairs with another alert naming the culprit; when it fires ALONE,
suspect the seams (gateway routing, seeded data missing after a nuke, auth).
**Fix**: per the failing step. `make seed` restores the demo world the
canary depends on. Verify: canary_runs_total{outcome="ok"} moving again.

## WorkerTargetAbsent

**Means**: no `order-worker` instance is being discovered at all. This fires
where `ServiceDown` structurally cannot: the worker jobs use `dns_sd_configs`,
so a dead container makes Docker DNS return nothing, Prometheus scrapes an
empty target list, and `up` disappears instead of going to 0. Absence is the
symptom for every discovery-based target. No worker means no saga advances —
placements answer `PlacementPending` (the workflow is durable and waits), the
kitchen's accept/reject signals queue, and nothing reaches SETTLED.
**Check**: `docker compose ps order-worker` (crash-loop shows recent restarts);
`make logs SVC=order-worker` for the exit reason — a sandbox import violation
in `workflows.py` and a failed Temporal connect are the two common ones.
Temporal UI :8233 confirms the diagnosis from the other side: workflows in
Running with activity tasks piling up unstarted.
**Fix**: repair the exit reason and restart the worker; the workflows resume
from their event history exactly where they stopped — nothing is lost, and
in-flight orders continue rather than needing replacement. Verify: the target
reappears within one scrape (15s) and `temporal_activity_schedule_to_start_latency`
p95 drains back toward baseline as the backlog clears.

## TracingDisarmed

**Means**: a process is serving traffic with span export switched off. Almost
always config drift, not a bug: `OTLP_ENDPOINT` is empty unless `make up-obs`
injects it, so any other `docker compose up`/`restart` that recreates a
container brings it back untraced. Metrics and logs keep flowing, which is what
makes this invisible without the gauge — you discover it during the incident
that needed the traces.
**Check**: `curl -s localhost:9090/api/v1/query?query=tracing_armed` names the
jobs reporting 0; confirm with
`docker inspect smartfoodops-<svc>-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep OTLP`.
**Fix**: `make up-obs` — it is the only target that sets the endpoint, and it
recreates the app tier with it. Verify: `tracing_armed` is 1 for every job, and
a fresh order produces a trace spanning all services rather than a partial one
(Jaeger → the `edge-bff POST /v1/orders` root should carry ~80 spans).

## RateLimitSpike

**Means**: one route class has been refusing requests with 429 at a sustained
rate — a credential-stuffing run against `auth`, a scraper against `read`, or
a broken client retry-looping against `write` (its own 429s feed the loop).
The limiter is WORKING; the question is who is hitting it.
**Check**: `sum by (route_class) (rate(rate_limited_total[5m]))` for the class;
then edge-bff logs for the scope — authed abuse logs the `sub`, anonymous
logs the first X-Forwarded-For hop (trustworthy only because the gateway in
front of the edge sets it; if the edge is ever exposed directly, that hop
becomes attacker-controlled and this runbook needs a redesign, not a tweak).
`rate_limit_errors_total` moving instead means Redis is unhealthy and every
check is failing OPEN — the limiter is off while it climbs.
**Fix**: abusive `sub` → suspend the account; abusive IP → block upstream of
the edge; legitimate bursts (a launch, a demo) → raise the class budget in
edge-bff config (`rate_limit_*_per_window`) — a restart-only change. Verify:
the class's refusal rate returns to zero without the success rate dropping.

## ReceiptsSweeperBusy

**Signal**: `receipts_swept_total` moved — the beat sweeper found receipts past
the grace window with no `delivery_log` row and re-enqueued their chains.

**Meaning**: the DIRECT path failed for those orders: either the post-commit
enqueue was dropped (check `receipt_enqueue_failures_total` on notification —
nonzero means RabbitMQ was unreachable at settle time) or sends are stalling
longer than the grace window (check receipt-sender logs for `Retry in …` and
mock-mailer/provider health). The sweeper is the designed repair, so customers
still get receipts — this alert is about WHY the repair keeps being needed.

**Actions**: `docker logs smartfoodops-receipt-sender-1 | grep Retry` for the
failure; RabbitMQ UI (:15672) for queue depth; if the broker was down, confirm
it is back and the counter stops moving. Duplicate emails are possible during
overlap windows and are the accepted trade (ADR-0025).

## ReceiptParked

**Signal**: `receipts_sent_total{outcome=~"rejected|no_recipient"}` moved —
either the provider answered 4xx (`rejected`) or Identity has no such user
for a settled order (`no_recipient`, a data bug worth investigating on its
own). Both move the receipt to `status = 'parked'`, which takes it out of the
sweeper's partial index entirely. Nothing automatic will retry it, on
purpose: retrying a rejected request or a nonexistent user cannot change
the answer.

**Actions**: find it (`SELECT order_id, created_at FROM receipts WHERE
status = 'parked'`), fix the cause (bad recipient mapping, oversized body),
then un-park it — `UPDATE receipts SET status = 'pending' WHERE order_id =
…` — and the next sweep re-enqueues it. That UPDATE is the replay lever,
the same shape as a DLQ replay.

---

# The assistant plane (B7)

Dashboard: http://localhost:3000/d/smartfoodops-assistant

Two things to hold in mind before paging anyone. The assistant is a **99.5%
plane beside a 99.95% ordering path** (NFR-23) — degraded answers are not a
degraded checkout, and **order placement is unaffected by any AI failure**.
And every feature has a defined non-AI answer (NFR-29): lexical search,
popularity recommendations, templated explanations. So the first question on
any assistant alert is not "how do we restore the model", it is "did the
fallback take, and is anyone actually stuck?"

## AssistantTimeToFirstTokenBreach

**Means**: p95 time-to-first-token is over NFR-21's 1.5s. This is the wait a
customer stares at before any text appears; the full answer latency is a
separate alert. A streamed answer's HTTP duration is its connection lifetime,
not its latency, which is why stream routes are kept out of the HTTP
histogram and measured here instead.
**Check**: whether the answer-latency panel moved too (both → the provider;
only this one → retrieval or prompt assembly in front of the first token);
`assistant_context_truncations_total` rising means prompts are hitting the
input cap and being trimmed, which costs time before the call; one slow trace
in Jaeger — graph nodes are spans, so the fat one names the node.
**Fix**: provider-side → the failover panel should already show the secondary
taking over; retrieval-side → see **AssistantKnowledgeStale** for index
health. Verify: p95 bends back under 1.5s within one 10m window.

## AssistantAnswerLatencyBreach

**Means**: complete answers (`task="generate"` only) are breaching NFR-21's
6s p95. `explain` is excluded deliberately — it is templated and fast, and
averaging the two lets the fast path hide the slow one.
**Check**: the cache hit-rate panel first. A hit answers in milliseconds, so
a hit-rate collapse raises this number without anything being slower — the
two alerts fire together and the cache one is the cause. Then the failover
panel: a sustained failover means every answer pays a retry before it starts.
**Fix**: cache-driven → **AssistantCacheHitRateCollapsed**; provider-driven →
**AssistantProviderFailoversSustained**. Verify: p95 under 6s.

## AssistantUngroundedAnswer

**Means**: an answer **that shipped to a customer** cited something that was
never retrieved. This counter is post-validation leakage, so one is not
noise — NFR-26 requires 100% of item references in shipped answers to resolve
to real, currently-available items. A customer has been told about a dish
that may not exist, at a price we did not quote from the live snapshot.
**Check**: the grounding panel on the AI dashboard — `dropped_ungrounded`
rising alongside says retrieval is feeding the model unsupportable context
and some got through; flat there with this firing says the validator itself
let something past. Find the turn: `assistant_facts` rows with `ungrounded >
0` in the window give you `message_id`, then the trace by `trace_id`.
**Fix**: this is a correctness incident, not a capacity one. If the validator
is the gap, that is a code fix and the honest interim is to force the non-AI
answer for the affected surface (NFR-29's ladder) rather than keep shipping
unverified claims. Verify: no increase over a full 15m window.

## AssistantKnowledgeStale

**Means**: committed menu changes are taking longer than NFR-28's 60s p99 to
become visible to retrieval. This is the failure mode that does not look like
one: the assistant keeps answering confidently, about a menu the kitchen has
already changed. Prices, availability and new dishes are all affected.
**Check**: `assistant_knowledge_drain_failures_total` (its own alert below) —
a failing drain is the usual cause and the row stays queued, so the backlog
grows rather than losing work; the pending queue depth in `assistant_db`;
whether the embedding provider is answering at all
(`assistant_embed_requests_total` flat while changes keep arriving).
**Fix**: restart the drain worker if it is wedged; if the embedding provider
is the blocker the index simply cannot advance, and the call to make is
whether to keep serving a stale index or drop that surface to its non-AI
fallback. Verify: p99 back under 60s and the pending queue draining.

## AssistantKnowledgeDrainFailing

**Means**: drain passes are raising repeatedly. The row **stays queued** when
a pass fails, so this is a retry rate and not a loss rate — nothing has been
dropped, it is just not arriving. Warn rather than page for exactly that
reason; sustained, it becomes **AssistantKnowledgeStale**.
**Check**: `docker compose logs ai-assistant-worker` for the raised
exception — embedding provider errors, a DB connection, or a poison row that
fails every pass.
**Fix**: per the exception. A poison row that fails forever is the one case
worth parking by hand so the rest of the queue moves. Verify: failures stop
and the freshness p99 recovers.

## AssistantProviderFailoversSustained

**Means**: the assistant is routinely retrying on its secondary provider
(ADR-0030 §4). **One failover is the design working** — the secondary caught
it and the customer got an answer. A sustained rate means the primary is
effectively down and every answer is paying a failed call before it starts,
which shows up as latency, cost and quota pressure.
**Check**: the `from_provider`/`to_provider`/`reason` labels name both ends
and the cause (timeout, quota, error); whether the secondary's own latency is
acceptable on the answer-latency panel.
**Fix**: if the primary is down upstream there is nothing to fix here and the
secondary is doing its job — the decision is whether to promote it and stop
paying the retry. If it is quota, that is NFR-24's binding constraint, not an
outage. Verify: the failover rate returns to near zero.

## AssistantSheddingCustomers

**Means**: calls are being refused **before reaching a provider** (FR-102's
budgets). The `reason` label is the whole diagnosis: a per-user budget
catching one heavy user is the mechanism working as designed, and a cell-wide
budget refusing everyone is an outage wearing a polite message.
**Check**: `sum by (reason)` on the refusals panel; if it is cell-wide, the
token spend that drove it (the cost panels) and whether a single user or a
loop is responsible.
**Fix**: per-user → nothing, unless the limit is wrong. Cell-wide → either
raise the budget deliberately or accept the shed and confirm customers are
landing on the non-AI answer rather than a blank screen (NFR-29). Verify:
refusals stop, or the reason narrows to per-user.

## AssistantCacheHitRateCollapsed

**Means**: the answer cache is serving under 20% of lookups. NFR-24 names the
cache tiers **load-bearing rather than an optimisation** — at the ceiling,
provider quota is the binding constraint and the hit rate is what keeps
generation volume inside it. So this is a cost and capacity incident, not a
slow day, and it will drag **AssistantAnswerLatencyBreach** with it.
**Check**: both tiers on the panel — `exact` collapsing alone suggests
question text changed shape (a new entry point, a client appending something
per-request); `semantic` collapsing too suggests the embedding path or the
index behind it; a recent deploy that changed the cache key or a reindex that
changed `model_version`.
**Fix**: if a key change caused it the cache is cold and will refill — confirm
the rate is climbing rather than flat. A genuinely broken tier is a code fix;
until then watch quota. Verify: hit rate climbing back over 20%.

## AssistantWorkerTargetAbsent

**Means**: Prometheus is discovering **no** `ai-assistant-worker` instance.
Note the shape: this is `absent()`, not `up == 0`, and that is the whole
point. The worker is a dns_sd target, so a dead container means Docker DNS
returns nothing, Prometheus reads an empty target list, and `up` goes away
rather than going to zero — `ServiceDown` matches nothing at all. Absence is
the symptom for every discovery-based target (same trap as
**WorkerTargetAbsent**).
**Check**: `docker compose ps ai-assistant-worker` — on this box a recurring
cause is an OOM kill (exit 137) rather than a crash; the content-studio
queue depth in `assistant_db` (`content_drafts` rows still `queued`).
**Fix**: restart the worker. Nothing is lost — Celery is `acks_late`, so an
in-flight job is redelivered, and the drafts the shed parked are replayable.
Verify: the target reappears within one scrape and queued rows start moving.

## AssistantFactsLagging

**Means**: `analytics.assistant.v1` is behind on `c1.assistant.events`. This
is the alert that decides whether **every other number on the AI dashboard
can be trusted**. Nothing errors when this projection stalls: the panels keep
rendering, the time window keeps sliding, and the numbers quietly describe a
shorter period than their label claims. Usage, answer rate, response time,
acceptance and conversion are all read from these facts.
**Check**: is analytics up and is the consumer running
(`docker compose logs analytics | grep "assistant facts folded"`); the lag
panel's trend — flat-and-large is a stopped consumer, growing is a consumer
too slow for the rate.
**Fix**: restart analytics for a stopped consumer. Catch-up is safe and needs
no care: the facts carry absolute values keyed by `message_id`, so replaying
the backlog converges to the same rows rather than inflating anything.
Verify: lag returns toward zero and the interactions panel fills the gap in
retrospect — it genuinely backfills, because the rows are facts and not
counters.

## AssistantCostPerOrderElevated

**Means**: AI spend has reached 60% of the $0.004-per-order budget
(capacity-plan §7.4, A23). Per this repo's standing rule, 60% is a **planned
action with slack**, not an incident — nothing is broken yet.
**Check**: the **Tokens per order** panel first, because it separates the two
causes that look identical on the cost line. Tokens per order rising means
*we* changed — a longer prompt, more retrieved context packed in, a cache
regression. Tokens per order flat with cost rising means a **vendor
repriced**, or routing shifted to a more expensive model, and that is not an
engineering regression. Then the cache hit-rate panel: a collapse raises cost
and latency together.
**Fix**: cache-driven → **AssistantCacheHitRateCollapsed**. Prompt-driven →
the context cap (`assistant_context_truncations_total` shows whether it is
engaging). Vendor-driven → this is a budget decision, not a fix; update A23
and the price table deliberately rather than silencing the alert. Verify:
back under $0.0024/order.

## AssistantCostPerOrderOverBudget

**Means**: AI spend is **over** the $0.004-per-order budget. The budget was
set at ~60× the modelled flash-class cost and ~2.4× premium-class
specifically so a deliberate model upgrade would not reach it — so this
firing means either a runaway or a decision nobody recorded.
**Check**: everything under **AssistantCostPerOrderElevated**, plus the spend
panel split by model — a model appearing that nobody deployed is routing gone
wrong; one model's completion tokens dominating suggests `max_output_tokens`
is not being applied.
**Fix**: the lever that stops spend immediately is the degradation ladder's
generation switch (`generation=off`, NFR-29 step 2a) — it is reversible, it
now reaches the content worker as well as chat, and every surface has a
defined non-AI answer. Throwing it costs answer quality, not checkout.
Verify: spend rate drops within one 10m window; drafts park with "generation
is switched off" rather than failing.

## AssistantSpendUnpriced

**Means**: tokens are being billed by a model that has **no row in the price
table** (`deploy/compose/observability/llm-costs.yml`). This is the one
failure mode of the price-table design and it is nastier than it sounds: the
unpriced model drops out of the join, so the cost panels keep rendering a
smaller number **with no gap in them**. Every cost tripwire is blind for that
model until this is fixed — they are not firing because they cannot see it.
**Check**: the unpriced-tokens panel names the `model` and `direction`. It is
usually a model id that changed under a floating alias, or a newly routed
fallback nobody added a price for.
**Fix**: add the row to `llm-costs.yml` from the vendor's published price
list and reload Prometheus. Verify: the unpriced panel returns to empty and
the cost-per-order line steps UP to its true value — expect that step, it is
the correction, not a regression.

## Deploying B7 to an existing analytics database

Not an alert — a one-time step, recorded here because its symptom looks like
a bug. FR-98 scopes a restaurant admin's AI insights through
`restaurant_brands`, which is populated by the `catalog.changes` consumer. On
an existing deployment that group has **already committed its offsets**, so
after migration 0008 the table stays empty and every brand owner sees zero
AI-driven views while their conversions still work.

The mapping is naturally idempotent and the topic is compacted, so replaying
it converges — that replay *is* the backfill:

```bash
docker stop smartfoodops-analytics-1
docker compose -f deploy/compose/docker-compose.yml exec -T kafka \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group analytics.brand-repoint --reset-offsets --to-earliest --all-topics --execute
docker start smartfoodops-analytics-1
```

Verify: `select count(*) from restaurant_brands;` is non-zero and roughly the
number of branches catalog knows. A fresh deployment needs none of this — the
consumer reads the compacted topic from the start on its first run.
